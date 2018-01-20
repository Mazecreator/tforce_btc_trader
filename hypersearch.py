import argparse, json, math, time, pdb
from pprint import pprint
from box import Box
import numpy as np
import pandas as pd
import tensorflow as tf
from sqlalchemy.sql import text
from tensorforce.agents import agents as agents_dict
from tensorforce.core.networks import layer as TForceLayers
from tensorforce.core.networks.network import LayeredNetwork
from sklearn.ensemble import GradientBoostingRegressor
from sklearn.model_selection import GridSearchCV

from btc_env import BitcoinEnv
import utils
from data import data

"""
Each hyper is specified as `key: {type, vals, guess, pre/post/hydrate}`. 
- type: (int|bounded|bool). bool is True|False param, bounded is a float between min & max, int is "choose one" 
    eg 'activation' one of (tanh|elu|selu|..)`)
- vals: the vals this hyper can take on. If type(vals) is primitive, hard-coded at this value. If type is list, then
    (a) min/max specified inside (for bounded); (b) all possible options (for 'int'). If type is dict, then the keys
    are used in the searching (eg, look at the network hyper) and the values are used as the configuration.
- guess: initial guess (supplied by human) to explore      
- pre/post/hydrate: hooks that transform this hyper before plugging it. Eg, we'd use type='bounded' for batch size since
    we want to range from min to max (instead of listing all possible values); but we'd cast it to an int inside
    hook before using it.
    - pre: transform it immediately, it'll be saved in the runs table this way
    - post: transform it after all the pre-hooks are run, in case this depends on other hypers
    - hydrate: big-time transformation to the whole hypers dict, based on this hyper val. It won't be saved to the 
        database looking like this. Eg, baseline_mode, when set to True, does a number on many other hypers. 

The special sauce is specifying hypers as dot-separated keys, like `memory.type`. This allows us to easily 
mix-and-match even within a config-dict. Eg, you can try different combos of hypers within `memory{}` w/o having to 
specify the whole block combo (`memory=({this1,that1}, {this2,that2})`). To use this properly, make sure to specify
a `requires` field where necessary. 
"""


def build_net_spec(hypers, baseline=False):
    """Builds a net_spec from some specifications like width, depth, etc"""
    net, indicators, arbitrage = Box(hypers['net']), hypers['indicators'], hypers['arbitrage']

    dense = {'type': 'dense', 'activation': net.activation, 'l2_regularization': net.l2, 'l1_regularization': net.l1}
    dropout = {'type': 'dropout', 'rate': net.dropout}
    lstm = {'type': 'internal_lstm', 'dropout': net.dropout}
    conv2d = {
        'type': 'conv2d',
        # 'bias': net.bias,
        'l2_regularization': net.l2,
        'l1_regularization': net.l1
    }

    arr = []

    # Pre-layer
    if 'depth_pre' in net:
        for i in range(net.depth_pre):
            size = int(net.width/(net.depth_pre-i+1)) if net.funnel else net.width
            arr.append({'size': size, **dense})
            if net.dropout: arr.append({**dropout})

    # Mid-layer
    # TODO figure out how to use internals w/ baseline, currently not series-aware
    if not (net.type == 'lstm' and baseline):
        if net.type == 'conv2d':
            n_cols = data.n_cols(indicators=indicators, arbitrage=arbitrage)
            steps_out = hypers['step_window']

        for i in range(net.depth_mid):
            if net.type == 'lstm':
                # arr.append({'size': net.width, 'return_final_state': (i == net.depth-1), **lstm})
                arr.append({'size': net.width, **lstm})
                continue

            step_window = math.ceil(steps_out / (net.window * 10))
            step_stride = math.ceil(step_window / net.stride)

            # next = (length - window)/stride + 1
            steps_out = (steps_out - step_window)/step_stride + 1

            # TODO this is ugly
            # Ensure there's some minimal amount of reduction at the lower levels (else, we get layers that map 1-1 to next layer)
            min_window, min_stride = 3, 2
            step_window = max([step_window, min_window])
            step_stride = max([step_stride, min_stride])

            size = max([32, int(net.width / 4)])
            # if i == 0: size = int(size / 2)  # Most convs have their first layer smaller... right? just the first, or what?
            arr.append({
                'size': size,
                'window': (step_window, 1),
                'stride': (step_stride, 1),
                **conv2d
            })
        if net.type == 'conv2d':
            arr.append({'type': 'flatten'})

    # Dense
    for i in range(net.depth_post):
        size = int(net.width / (i + 1)) if net.funnel else net.width
        arr.append({'size': size, **dense})
        if net.dropout: arr.append({**dropout})

    return arr


def custom_net(hypers, print_net=False, baseline=False):
    layers_spec = build_net_spec(hypers, baseline)
    if print_net: pprint(layers_spec)

    class CustomNet(LayeredNetwork):
        def __init__(self, **kwargs):
            super(CustomNet, self).__init__(layers_spec, **kwargs)

        def tf_apply(self, x, internals, update, return_internals=False):
            series = x['series']
            stationary = x['stationary']
            x = series

            # Apply stationary to the first Dense after the last LSTM. in the case of Baseline, there's no LSTM,
            # so apply it to the start
            apply_stationary_here = 0
            # Find the last LSTM layer, peg to the next layer (a Dense)
            for i, layer in enumerate(self.layers):
                if isinstance(layer, TForceLayers.InternalLstm) or isinstance(layer, TForceLayers.Flatten):
                    apply_stationary_here = i + 1

            internal_outputs = list()
            index = 0
            for i, layer in enumerate(self.layers):
                layer_internals = [internals[index + n] for n in range(layer.num_internals)]
                index += layer.num_internals
                if i == apply_stationary_here:
                    x = tf.concat([x, stationary], axis=1)
                x = layer.apply(x, update, *layer_internals)

                if not isinstance(x, tf.Tensor):
                    internal_outputs.extend(x[1])
                    x = x[0]

            if return_internals:
                return x, internal_outputs
            else:
                return x
    return CustomNet


def bins_of_8(x): return int(x // 8) * 8

def two_to_the(x, _): return 2**x

def ten_to_the_neg(x, _): return 10**-x

def min_threshold(thresh, fallback):
    return lambda x, _: x if (x and x > thresh) else fallback

def min_ten_neg(thresh, fallback):
    return lambda x, _: min_threshold(thresh, fallback)(ten_to_the_neg(x, _), _)

def hydrate_baseline(x, flat):
    return {
        False: {'baseline_mode': None},
        True: {
            'baseline': {'type': 'custom'},
            'baseline_mode': 'states',
            'baseline_optimizer': {
                'type': 'multi_step',
                'num_steps': flat['optimization_steps'],
                'optimizer': {
                    'type': flat['step_optimizer.type'],
                    'learning_rate': 10 ** -flat['step_optimizer.learning_rate']
                }
            },
        }
    }[x]

hypers = {}
hypers['agent'] = {}
hypers['batch_agent'] = {
    'batch_size': {
        'type': 'bounded',
        'vals': [3, 11],
        'guess': 10,  # 1024
        'pre': round,
        'hydrate': two_to_the
    },
    'keep_last_timestep': {
        'type': 'bool',
        'guess': True
    }
}
hypers['model'] = {
    'optimizer.type': {
        'type': 'int',
        'vals': ['nadam', 'adam'],
        'guess': 'adam'
    },
    'optimizer.learning_rate': {
        'type': 'bounded',
        'vals': [0., 8.],
        'guess': 6.5,
        'hydrate': ten_to_the_neg
    },
    'optimization_steps': {
        'type': 'bounded',
        'vals': [1, 30],  # want to try higher, but too slow to test
        'guess': 10,
        'pre': round
    },
    'discount': {
        'type': 'bounded',
        'vals': [.9, .99],
        'guess': .99
    },
    # TODO variable_noise
}
hypers['distribution_model'] = {
    'entropy_regularization': {
        'type': 'bounded',
        'vals': [0, 5],
        'guess': 2,
        'hydrate': min_ten_neg(1e-4, 0.)
    }
}
hypers['pg_model'] = {
    'baseline_mode': {
        'type': 'bool',
        'guess': True,
        'hydrate': hydrate_baseline
    },
    'gae_lambda': {
        'type': 'bounded',
        'vals': [.8, 1.],
        'guess': .95,
        'post': lambda x, others: x if (x and x > .9 and others['baseline_mode']) else None
    },
}
hypers['pg_prob_ration_model'] = {
    'likelihood_ratio_clipping': {
        'type': 'bounded',
        'vals': [0., 1.],
        'guess': .2,
        'hydrate': min_threshold(.05, None)
    }
}

hypers['ppo_agent'] = {  # vpg_agent, trpo_agent
    **hypers['agent'],
    **hypers['batch_agent'],
    **hypers['model'],
    **hypers['distribution_model'],
    **hypers['pg_model'],
    **hypers['pg_prob_ration_model']

}
hypers['ppo_agent']['step_optimizer.learning_rate'] = hypers['ppo_agent'].pop('optimizer.learning_rate')
hypers['ppo_agent']['step_optimizer.type'] = hypers['ppo_agent'].pop('optimizer.type')

hypers['custom'] = {
    'indicators': {
        'type': 'bool',
        'guess': True
    },
    'net.depth_mid': {
        'type': 'bounded',
        'vals': [1, 3],
        'guess': 3,
        'pre': round
    },
    'net.depth_post': {
        'type': 'bounded',
        'vals': [1, 3],
        'guess': 2,
        'pre': round
    },
    'net.width': {
        'type': 'bounded',
        'vals': [3, 9],
        'guess': 6,  # 64
        'pre': round,
        'hydrate': two_to_the
    },
    'net.funnel': {
        'type': 'bool',
        'guess': True
    },
    'net.activation': {
        'type': 'int',
        'vals': ['tanh', 'relu'],
        'guess': 'tanh'
    },
    'net.dropout': {
        'type': 'bounded',
        'vals': [0., .5],
        'guess': .001,
        'hydrate': min_threshold(.1, None)
    },
    'net.l2': {
        'type': 'bounded',
        'vals': [0, 7],
        'guess': 7,  # turn off. setting to 0 causes issues in GP code
        'hydrate': min_ten_neg(1e-6, 0.)
    },
    'net.l1': {
        'type': 'bounded',
        'vals': [0, 7],
        'guess': 3,
        'hydrate': min_ten_neg(1e-6, 0.)
    },
    'pct_change': {
        'type': 'bool',
        'guess': False
    },
    'unimodal': {
        'type': 'bool',
        'guess': False
    },
    'scale': {
        'type': 'bool',
        'guess': True
    },
    # Repeat-actions intervention: double the reward (False), or punish (True)?
    'punish_repeats': {
        'type': 'bool',
        'guess': False
    },
    'arbitrage': {
        'type': 'bool',
        'guess': True
    }
}

hypers['lstm'] = {
    'net.depth_pre': {
        'type': 'bounded',
        'vals': [0, 3],
        'guess': 2,
        'pre': int
    },
}
hypers['conv2d'] = {
    # 'net.bias': True,
    'net.window': {
        'type': 'bounded',
        # window t-shirt sizes, smaller # = more destructive
        'vals': [1, 3],
        'guess': 2,
        'pre': round,
    },
    'net.stride': {
        'type': 'bounded',
        # how many ways to divide a window? 1 = no-overlap, 2 = half-overlap (smaller # = more destructive)
        'vals': [1, 3],
        'guess': 3,
        'pre': round
    },
    'step_window': {
        'type': 'bounded',
        'vals': [100, 400],
        'guess': 200,
        'pre': round,
    }
}


# Fill in implicit 'vals' (eg, 'bool' with [True, False])
for _, section in hypers.items():
    for k, v in section.items():
        if type(v) != dict: continue  # hard-coded vals
        if v['type'] == 'bool': v['vals'] = [0, 1]

class DotDict(object):
    """
    Utility class that lets you get/set attributes with a dot-seperated string key, like `d = a['b.c.d']` or `a['b.c.d'] = 1`
    """
    def __init__(self, obj):
        self._data = obj
        self.update = self._data.update

    def __getitem__(self, path):
        v = self._data
        for k in path.split('.'):
            if k not in v:
                return None
            v = v[k]
        return v

    def __setitem__(self, path, val):
        v = self._data
        path = path.split('.')
        for i, k in enumerate(path):
            if i == len(path) - 1:
                v[k] = val
                return
            elif k in v:
                v = v[k]
            else:
                v[k] = {}
                v = v[k]

    def to_dict(self):
        return self._data


class HSearchEnv(object):
    """
    This is the "wrapper" environment (the "inner" environment is the one you're testing against, like Cartpole-v0).
    This env's actions are all the hyperparameters (above). The state is nothing (`[1.]`), and a single episode is
    running the inner-env however many episodes (300). The inner's last-few reward avg is outer's one-episode reward.
    That's one run: make inner-env, run 300, avg reward, return that. The next episode will be a new set of
    hyperparameters (actions); run inner-env from scratch using new hypers.
    """
    def __init__(self, agent='ppo_agent', gpu_split=1, net_type='conv2d'):
        """
        TODO only tested with ppo_agent. There's some code for dqn_agent, but I haven't tested. Nothing else
        is even attempted implemtned
        """
        hypers_ = hypers[agent].copy()
        hypers_.update(hypers['custom'])
        hypers_['net.type'] = net_type  # set as hard-coded val
        hypers_.update(hypers[net_type])

        hardcoded = {}
        for k, v in hypers_.copy().items():
            if type(v) != dict: hardcoded[k] = v

        self.hypers = hypers_
        self.agent = agent
        self.hardcoded = hardcoded
        self.gpu_split = gpu_split
        self.net_type = net_type
        self.conn = data.engine.connect()
        self.conn_runs = data.engine_runs.connect()

    def close(self):
        self.conn.close()
        self.conn_runs.close()

    def get_hypers(self, actions):
        """
        Bit of confusing logic here where I construct a `flat` dict of hypers from the actions - looks like how hypers
        are specified above ('dot.key.str': val). Then from that we hydrate it as a proper config dict (hydrated).
        Keeping `flat` around because I save the run to the database so can be analyzed w/ a decision tree
        (for feature_importances and the like) and that's a good format, rather than a nested dict.
        :param actions: the hyperparamters
        """
        self.flat = flat = {}
        # Preprocess hypers
        for k, v in actions.items():
            try: v = v.item()  # sometimes primitive, sometimes numpy
            except Exception: pass
            hyper = self.hypers[k]
            if 'pre' in hyper:
                v = hyper['pre'](v)
            flat[k] = v
        flat.update(self.hardcoded)

        # Post-process hypers (allow for dependency handling, etc)
        for k, v in flat.items():
            hyper = self.hypers[k]
            if type(hyper) == dict and 'post' in hyper:
                flat[k] = hyper['post'](v, flat)

        # change all a.b=c to {a:{b:c}} (note DotDict class above, I hate and would rather use an off-the-shelf)
        main, custom = DotDict({}), DotDict({})
        for k, v in flat.items():
            obj = main if k in hypers[self.agent] else custom
            try:
                v = self.hypers[k]['hydrate'](v, self.flat)
                if type(v) == dict: obj.update(v)
                else: obj[k] = v
            except: obj[k] = v
        main, custom = main.to_dict(), custom.to_dict()

        network = custom_net(custom, print_net=True)
        if flat['baseline_mode']:
            if type(self.hypers['baseline_mode']) == bool:
                main.update(hydrate_baseline(self.hypers['baseline_mode'], flat))

            main['baseline']['network_spec'] = custom_net(custom, baseline=True)

        # GPU split
        session_config = None
        if self.gpu_split != 1:
            fraction = .9 / self.gpu_split if self.gpu_split > 1 else self.gpu_split
            session_config = tf.ConfigProto(gpu_options=tf.GPUOptions(per_process_gpu_memory_fraction=fraction))
        main['session_config'] = session_config

        print('--- Flat ---')
        pprint(flat)
        print('--- Hydrated ---')
        pprint(main)

        return flat, main, network

    def execute(self, actions):
        flat, hydrated, network = self.get_hypers(actions)

        env = BitcoinEnv(flat, name=self.agent)
        agent = agents_dict[self.agent](
            states_spec=env.states,
            actions_spec=env.actions,
            network_spec=network,
            **hydrated
        )

        env.train_and_test(agent)

        step_acc, ep_acc = env.acc.step, env.acc.episode
        adv_avg = ep_acc.advantages[-1]
        print(flat, f"\nAdvantage={adv_avg}\n\n")

        sql = """
          insert into runs (hypers, advantage_avg, advantages, uniques, prices, actions, agent, flag) 
          values (:hypers, :advantage_avg, :advantages, :uniques, :prices, :actions, :agent, :flag)
        """
        self.conn_runs.execute(
            text(sql),
            hypers=json.dumps(flat),
            advantage_avg=adv_avg,
            advantages=list(ep_acc.advantages),
            uniques=list(ep_acc.uniques),
            prices=list(env.prices),
            actions=list(step_acc.signals),
            agent=self.agent,
            flag=self.net_type
        )

        agent.close()
        env.close()
        return adv_avg

    def get_winner(self, id=None):
        if id:
            sql = "select id, hypers from runs where id=:id"
            winner = self.conn_runs.execute(text(sql), id=id).fetchone()
            winner = winner.hypers
            print(winner)
        else:
            winner = {}
            for k,v in self.hypers.items():
                if k not in self.hardcoded:
                    winner[k] = v['guess']
            winner.update(self.hardcoded)
        self.hardcoded = winner
        return self.get_hypers({})


def print_feature_importances(X, Y, feat_names):
    if len(X) < 5: return
    model = GradientBoostingRegressor()
    model_hypers = {
        'max_features': [None, 'sqrt', 'log2'],
        'max_depth': [None, 10, 20],
        'n_estimators': [100, 200, 300],
    }
    model = GridSearchCV(model, param_grid=model_hypers, cv=5, scoring='neg_mean_squared_error', n_jobs=-1)
    model.fit(X, np.squeeze(Y))
    feature_imp = sorted(zip(model.best_estimator_.feature_importances_, feat_names), key=lambda x: x[0],
                         reverse=True)
    print('\n\n--- Feature Importances ---\n')
    print('\n'.join([f'{x[1]}: {round(x[0],4)}' for x in feature_imp]))
    return model


def boost_optimization(model, loss_fn, bounds, x_list=[], y_list=[], n_pre_samples=5):
    # Handle any specifically-asked for "guesses" first
    for i, v in enumerate(y_list):
        if v[0] is None:
            print("Running guess values")
            y_list[i] = loss_fn(x_list[i])

    n_pre_samples -= len(x_list)
    if n_pre_samples > 0:
        for params in np.random.uniform(bounds[:, 0], bounds[:, 1], (n_pre_samples, bounds.shape[0])):
            x_list.append(params)
            y_list.append(loss_fn(params))

    best_params, best_score = None, -1000
    for params in np.random.uniform(bounds[:, 0], bounds[:, 1], (int(1e6), bounds.shape[0])):
        prediction = model.predict([params])[0]
        if prediction > best_score:
            best_params = params
            best_score = prediction
    loss_fn(best_params)


def main_gp():
    import gp
    from sklearn.feature_extraction import DictVectorizer

    parser = argparse.ArgumentParser()
    parser.add_argument('-g', '--gpu-split', type=float, default=1, help="Num ways we'll split the GPU (how many tabs you running?)")
    parser.add_argument('-n', '--net-type', type=str, default='conv2d', help="(lstm|conv2d) Which network arch to use")
    parser.add_argument('--guess', type=int, default=-1, help="Run the hard-coded 'guess' values first before exploring")
    parser.add_argument('--boost', action="store_true", default=False, help="Use custom gradient-boosting optimization, or bayesian optimization?")
    args = parser.parse_args()

    # Encode features
    hsearch = HSearchEnv(gpu_split=args.gpu_split, net_type=args.net_type)
    hypers_, hardcoded = hsearch.hypers, hsearch.hardcoded
    hypers_ = {k: v for k, v in hypers_.items() if k not in hardcoded}
    hsearch.close()

    # Build a matrix of features,  length = max feature size
    max_num_vals = 0
    for v in hypers_.values():
        l = len(v['vals'])
        if l > max_num_vals: max_num_vals = l
    empty_obj = {k: None for k in hypers_}
    mat = pd.DataFrame([empty_obj.copy() for _ in range(max_num_vals)])
    for k, hyper in hypers_.items():
        for i, v in enumerate(hyper['vals']):
            mat.loc[i,k] = v
    mat.ffill(inplace=True)

    # Above is Pandas-friendly stuff, now convert to sklearn-friendly & pipe through OneHotEncoder
    vectorizer = DictVectorizer()
    vectorizer.fit(mat.T.to_dict().values())
    feat_names = vectorizer.get_feature_names()

    # Map TensorForce actions to GP-compatible `domain`
    # instantiate just to get actions (get them from hypers above?)
    bounds = []
    for k in feat_names:
        hyper = hypers_.get(k, False)
        if hyper:
            bounded, min_, max_ = hyper['type'] == 'bounded', min(hyper['vals']), max(hyper['vals'])
        b = [min_, max_] if bounded else [0, 1]
        bounds.append(b)

    def hypers2vec(obj):
        h = dict()
        for k, v in obj.items():
            if k in hardcoded: continue
            if type(v) == bool: h[k] = float(v)
            else: h[k] = v or 0.
        return vectorizer.transform(h).toarray()[0]

    def vec2hypers(vec):
        # Reverse the encoding
        # https://stackoverflow.com/questions/22548731/how-to-reverse-sklearn-onehotencoder-transform-to-recover-original-data
        # https://github.com/scikit-learn/scikit-learn/issues/4414
        reversed = vectorizer.inverse_transform([vec])[0]
        obj = {}
        for k, v in reversed.items():
            if '=' not in k:
                obj[k] = v
                continue
            if k in obj: continue  # we already handled this x=y logic (below)
            # Find the winner (max) option for this key
            score, attr, val = v, k.split('=')[0], k.split('=')[1]
            for k2, score2 in reversed.items():
                if k2.startswith(attr + '=') and score2 > score:
                    score, val = score2, k2.split('=')[1]
            obj[attr] = val

        # Bools come in as floats. Also, if the result is False they don't come in at all! So we start iterate
        # hypers now instead of nesting this logic in reversed-iteration above
        for k, v in hypers_.items():
            if v['type'] == 'bool':
                obj[k] = bool(round(obj.get(k, 0.)))
        return obj

    # Specify the "loss" function (which we'll maximize) as a single rl_hsearch instantiate-and-run
    def loss_fn(params):
        hsearch = HSearchEnv(gpu_split=args.gpu_split, net_type=args.net_type)
        reward = hsearch.execute(vec2hypers(params))
        hsearch.close()
        return [reward]

    guess_i = 0
    while True:
        """
        Every iteration, re-fetch from the database & pre-train new model. Acts same as saving/loading a model to disk, 
        but this allows to distribute across servers easily
        """
        conn_runs = data.engine_runs.connect()
        sql = "select hypers, advantages, advantage_avg from runs where flag=:f"
        runs = conn_runs.execute(text(sql), f=args.net_type).fetchall()
        conn_runs.close()
        X, Y = [], []
        for run in runs:
            X.append(hypers2vec(run.hypers))
            Y.append([utils.calculate_score(run)])
        boost_model = print_feature_importances(X, Y, feat_names)

        if args.guess != -1:
            guess = {k: v['guess'] for k, v in hypers_.items()}
            guess_overrides = [
                [
                    {},
                    {'net.l1': 7., 'net.l2': 3., 'net.dropout': 0.5},  # l2 + dropout
                    {'pct_change': True},
                ],
                [
                    {'net.l1': 7., 'net.l2': 7., 'net.dropout': 0.001},  # none
                    {'optimization_steps': 20},
                    {'step_optimizer.learning_rate': 5.5},

                ],
                [
                    {'net.l1': 7., 'net.l2': 7., 'net.dropout': .5},  # dropout
                    {'net.width': 4},
                    {'net.width': 8},
                ],
                [
                    {'net.l1': 7., 'net.l2': 3., 'net.dropout': 0.001},  # l2
                    {'net.activation': 'relu'},
                    {'net.stride': 2},

                ],
                [
                    {'net.l1': 3., 'net.l2': 3., 'net.dropout': 0.5},  # all
                    {'step_window': 400},
                    {'unimodal': True},
                ],
                [
                    {'scale': False},
                    {'baseline_mode': False},
                    {'net.window': 1},
                ],
                [
                    {'net.window': 3},
                    {'net.depth_post': 1},
                    {'net.stride': 1},
                ],
                [
                    {'net.depth_mid': 2},
                    {'punish_repeats': True},
                    {'arbitrage': False},
                ],
                [
                    {'batch_size': 9},
                    {'batch_size': 11},
                    {'indicators': False},
                ],

            ]
            guess.update(guess_overrides[args.guess][guess_i])
            loss_fn(hypers2vec(guess))

            guess_i += 1
            if guess_i > len(guess_overrides[args.guess])-1:
                args.guess = -1  # start on GP

            continue

        if args.boost:
            print('Using gradient-boosting')
            boost_optimization(
                model=boost_model,
                loss_fn=loss_fn,
                bounds=np.array(bounds),
                x_list=X,
                y_list=Y
            )
        else:
            # Evidently duplicate values break GP. Many of these are ints, so they're definite duplicates. Either way,
            # tack on some small epsilon to make them different (1e-6 < gp.py's min threshold, make sure that #'s not a
            # problem). Subtracting since many vals are int()'d, which is floor()
            for x in X:
                for i, v in enumerate(x):
                    x[i] += np.random.random() * 1e-6
            gp.bayesian_optimisation2(
                loss_fn=loss_fn,
                bounds=np.array(bounds),
                x_list=X,
                y_list=Y
            )

if __name__ == '__main__':
    main_gp()
