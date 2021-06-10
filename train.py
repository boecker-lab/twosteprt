import logging
import numpy as np
import tensorflow as tf
import torch
from LambdaRankNN import RankNetNN
from mpnranker import MPNranker, train as mpn_train, predict as mpn_predict
from tensorboardX import SummaryWriter
from rdkit import rdBase
import pickle
import json
import os
import re
import contextlib
from tap import Tap
from typing import List, Literal, Optional, Union
import pandas as pd

from utils import BatchGenerator, Data
from features import features, parse_feature_spec
from evaluate import eval_, predict, export_predictions

logger = logging.getLogger('rtranknet')
info = logger.info

class TrainArgs(Tap):
    input: List[str]            # Either CSV or dataset ids
    model_type: Literal['ranknet', 'mpn'] = 'ranknet'
    feature_type: Literal['None', 'rdkall', 'rdk2d', 'rdk3d'] = 'rdkall' # type of features to use
    # training
    batch_size: int = 256
    epochs: int = 5
    early_stopping_patience: Optional[int] = None # stop training when val loss doesn't improve for this number of times
    test_split: float = 0.2
    val_split: float = 0.05
    device: Optional[str] = None  # either `mirrored` or specific device name like gpu:1 or None (auto)
    remove_test_compounds: List[str] = [] # remove compounds occuring in the specified (test) datasets
    exclude_compounds_list: Optional[str] = None # list of compounds to exclude from training
    learning_rate: float = 1e-3
    no_encoder_train: bool = False # don't train the encoder(embedding) layers
    # data
    isomeric: bool = False      # use isomeric data (if available)
    balance: bool = False       # balance data by dataset
    no_group_weights: bool = False # don't scale weights by number of dataset pairs
    void_rt: float = 0.0        # void time threshold; used for ALL datasets
    metadata_void_rt: bool = False # use t0 value from repo metadata (times 3)
    validation_datasets: List[str] = [] # datasets to use for validation (instead of split of training data)
    test_datasets: List[str] = [] # datasets to use for test (instead of split of training data)
    # features
    features: List[str] = []                                     # custom features
    standardize: bool = False                                    # standardize features
    reduce_features: bool = False                                    # standardize features
    num_features: Optional[int] = None
    # additional features
    comp_classes: bool = False  # use classyfire compound classes as add. features
    sysinfo: bool = False       # use column information as add. features
    columns_use_hsm: bool = False
    hsm_fields: List[str] = ['H', 'S*', 'A', 'B', 'C (pH 2.8)', 'C (pH 7.0)']
    custom_column_fields: List[str] = []
    fallback_column: str = 'Waters, ACQUITY UPLC BEH C18' # column data to use when needed and no data available; can also be 'average'
    fallback_metadata: str = '0045' # repository metadata to use when needed and no data available; can also be 'average'
    usp_codes: bool = False     # use column usp codes as onehot system features (only for `--sysinfo`)
    debug_onehot_sys: bool = False # onehot dataset encoding
    onehot_test_sets: List[str] = [] # test set IDs to include in onehot encoding
    add_descs: bool = False     # use additional stored descriptors (e.g, qm8)
    classes_l_thr: float = 0.005
    classes_u_thr: float = 0.25
    # model general
    sizes: List[int] = [10, 10] # hidden layer sizes
    encoder_size: int = 300     # MPNencoder size
    dropout_rate: float = 0.0
    # mpn model
    mpn_loss: Literal['margin', 'bce'] = 'margin'
    mpn_margin: float = 0.1
    # pairs
    epsilon: float = 0.5 # difference in evaluation measure below which to ignore falsely predicted pairs
    pair_step: int = 1
    pair_stop: Optional[int] = None
    use_weights: bool = False
    weight_steep: float = 4
    weight_mid: float = 0.75
    no_inter_pairs: bool = False # don't use pairs of compounds of different datasets
    no_intra_pairs: bool = False # don't use pairs of compounds of the same dataset
    max_pair_compounds: Optional[int] = None
    # data locations
    repo_root_folder: str = '/home/fleming/Documents/Projects/RtPredTrainingData/'
    add_desc_file: str = '/home/fleming/Documents/Projects/rtranknet/data/qm_merged.csv'
    cache_file: str = 'cached_descs.pkl'
    # output control
    verbose: bool = False
    no_progbar: bool = False
    run_name: Optional[str] = None
    export_rois: bool = False
    plot_weights: bool = False
    save_data: bool = False
    ep_save: bool = False       # save after each epoch (only for mpn models)

def generic_run_name():
    from datetime import datetime
    time_str = datetime.now().strftime('%Y%m%d_%H-%M-%S')
    return f'ranknet_{time_str}'


def preprocess(data: Data, args: TrainArgs):
    data.compute_features(**parse_feature_spec(args.feature_type), n_thr=args.num_features, verbose=args.verbose,
                          add_descs=args.add_descs, add_desc_file=args.add_desc_file)
    if (data.train_y is not None):
        # assume everything was computed, split etc. already
        return ((data.train_graphs, data.train_x, data.train_y),
                (data.val_graphs, data.val_x, data.val_y),
                (data.test_graphs, data.test_x, data.test_y))
    if (args.cache_file is not None and features.write_cache):
        info('writing cache, don\'t interrupt!!')
        pickle.dump(features.cached, open(args.cache_file, 'wb'))
    if args.debug_onehot_sys:
        sorted_dataset_ids = sorted(set(args.input) | set(args.onehot_test_sets))
        data.compute_system_information(True, sorted_dataset_ids, repo_root_folder=args.repo_root_folder)
    info('done. preprocessing...')
    if (graphs):
        data.compute_graphs()
    data.split_data((args.test_split, args.val_split))
    if (args.standardize):
        data.standardize()
    if (args.reduce_features):
        data.reduce_f()
    if (args.fallback_metadata == 'average' or args.fallback_column == 'average'):
        data.nan_columns_to_average()
    if (args.fallback_metadata == 'zeros' or args.fallback_column == 'zeros'):
        data.nan_columns_to_zeros()
    return data.get_split_data((args.test_split, args.val_split))

def prepare_tf_model(args: TrainArgs, input_size: int) -> RankNetNN:
    if (args.device is not None and re.match(r'[cg]pu:\d', args.device.lower())):
        print(f'attempting to use device {args.device}')
        strategy = tf.distribute.OneDeviceStrategy(f'/{args.device.lower()}')
        context = strategy.scope()
    elif (len([dev for dev in tf.config.get_visible_devices() if dev.device_type == 'GPU']) > 1
        or args.device == 'mirrored'):
        # more than one gpu -> MirroredStrategy
        print('Using MirroredStrategy')
        strategy = tf.distribute.MirroredStrategy()
        context = strategy.scope()
    else:
        context = contextlib.nullcontext()
    with context:
        v = tf.Variable(1.0)
        info(f'using {v.device}')
        return RankNetNN(input_size=input_size,
                         hidden_layer_sizes=args.sizes,
                         activation=(['relu'] * len(args.sizes)),
                         solver='adam',
                         dropout_rate=args.dropout_rate)

if __name__ == '__main__':
    args = TrainArgs().parse_args()
    if (args.run_name is None):
        run_name = generic_run_name()
    else:
        run_name = args.run_name
    # logging
    ch = logging.StreamHandler()
    ch.setFormatter(logging.Formatter('%(asctime)s %(name)s %(levelname)s: %(message)s', datefmt='%H:%M:%S'))
    logger.addHandler(ch)
    if (args.verbose):
        logger.setLevel(logging.INFO)
        fh = logging.FileHandler(run_name + '.log')
        fh.setLevel(logging.INFO)
        fh.setFormatter(logging.Formatter('%(asctime)s %(name)s %(levelname)s: %(message)s'))
        logger.addHandler(fh)
        ch.setLevel(logging.INFO)
    else:
        rdBase.DisableLog('rdApp.warning')
    if (args.cache_file is not None):
        features.write_cache = False # flag for reporting changes to cache
        info('reading in cache...')
        if (os.path.exists(args.cache_file)):
            features.cached = pickle.load(open(args.cache_file, 'rb'))
        else:
            features.cached = {}
            info('cache file does not exist yet')
    info('reading in data and computing features...')
    graphs = (args.model_type == 'mpn')
    ranker: Union[RankNetNN, MPNranker] = None
    # TRAINING
    if (len(args.input) == 1 and os.path.exists(input_ := args.input[0])):
        if (input_.endswith('.csv') or input_.endswith('.tsv')):
            print('input from CSV/TSV file')
            # csv file
            data = Data.from_raw_file(input_, void_rt=args.void_rt,
                                      graph_mode=graphs)
        elif (input_.endswith('.tf')):
            print('input is trained Tensorflow model')
            # tensorflow model
            ranker = tf.keras.models.load_model(input_)
            data = pickle.load(open(os.path.join(input_, 'assets', 'data.pkl'), 'rb'))
            config = json.load(open(os.path.join(input_, 'assets', 'config.json')))
        elif (input_.endswith('.pt')):
            print('input is trained PyTorch model')
            # pytorch/mpn model
            # ensure Data/config.json also exist
            assert os.path.exists(data_path := input_.replace('.pt', '_data.pkl'))
            assert os.path.exists(config_path := input_.replace('.pt', '_config.json'))
            if (not torch.cuda.is_available()):
                # might be a GPU trained model -> adapt
                ranker = torch.load(input_, map_location=torch.device('cpu'))
                ranker.encoder.device = torch.device('cpu')
            else:
                ranker = torch.load(input_)
            info('loaded model')
            data = pickle.load(open(data_path, 'rb'))
            if (not hasattr(data, 'void_info')):
                data.void_info = None
            info('loaded data')
            config = json.load(open(config_path))
        else:
            raise Exception(f'input {args.input} not supported')
    elif (all(re.match(r'\d{4}', i) for i in args.input)):
        print('input from repository dataset IDs')
        # dataset IDs (recommended)
        data = Data(use_compound_classes=args.comp_classes, use_system_information=args.sysinfo,
                    metadata_void_rt=args.metadata_void_rt,
                    classes_l_thr=args.classes_l_thr, classes_u_thr=args.classes_u_thr,
                    use_usp_codes=args.usp_codes, custom_features=args.features,
                    use_hsm=args.columns_use_hsm, repo_root_folder=args.repo_root_folder,
                    custom_column_fields=args.custom_column_fields or None,
                    hsm_fields=args.hsm_fields, graph_mode=graphs,
                    fallback_column=args.fallback_column,
                    fallback_metadata=args.fallback_metadata)
        for did in args.input:
            data.add_dataset_id(did,
                                repo_root_folder=args.repo_root_folder,
                                void_rt=args.void_rt,
                                isomeric=args.isomeric)
        for did in args.validation_datasets:
            data.add_dataset_id(did,
                                repo_root_folder=args.repo_root_folder,
                                void_rt=args.void_rt,
                                isomeric=args.isomeric,
                                split_type='val')
        for did in args.test_datasets:
            data.add_dataset_id(did,
                                repo_root_folder=args.repo_root_folder,
                                void_rt=args.void_rt,
                                isomeric=args.isomeric,
                                split_type='test')
        if (args.remove_test_compounds is not None and len(args.remove_test_compounds) > 0):
            d_temp = Data()
            for t in args.remove_test_compounds:
                d_temp.add_dataset_id(t, repo_root_folder=args.repo_root_folder,
                                      isomeric=args.isomeric)
            compounds_to_remove = set(d_temp.df['inchi.std'].tolist())
            data.df = data.df.loc[~data.df['inchi.std'].isin(compounds_to_remove)]
            print(f'removed {len(compounds_to_remove)} compounds occuring '
                  'in test data from training data')
        if (args.exclude_compounds_list is not None):
            # List of compounds, first line has to be either id, smiles.std, inchi.std, inchikey.std
            to_exclude = pd.read_csv(args.exclude_compounds_list)
            col = to_exclude.columns[0]
            prev_len = len(data.df)
            data.df = data.df.loc[~data.df[col].isin(to_exclude[col].tolist())]
            print(f'removed {prev_len - len(data.df)} compounds by column {col} '
                  f'from exclusion list (length {len(to_exclude)})')
        if (args.balance and len(args.input) > 1):
            data.balance()
            info('added data for datasets:\n' +
                 '\n'.join([f'  - {did} ({name})' for did, name in
                            set(data.df[['dataset_id', 'column.name']].itertuples(index=False))]))
    else:
        raise Exception(f'input {args.input} not supported')
    ((train_graphs, train_x, train_y),
     (val_graphs, val_x, val_y),
     (test_graphs, test_x, test_y)) = preprocess(data, args)
    info('done. Initializing BatchGenerator...')
    bg = BatchGenerator((train_graphs, train_x) if graphs else train_x, train_y,
                        ids=data.df.iloc[data.train_indices].smiles.tolist(),
                        batch_size=args.batch_size, pair_step=args.pair_step,
                        pair_stop=args.pair_stop, use_weights=args.use_weights,
                        use_group_weights=(not args.no_group_weights),
                        dataset_info=data.df.dataset_id.iloc[data.train_indices].tolist(),
                        void_info=data.void_info, weight_steep=args.weight_steep,
                        no_inter_pairs=args.no_inter_pairs,
                        no_intra_pairs=args.no_intra_pairs,
                        max_indices_size=args.max_pair_compounds,
                        weight_mid=args.weight_mid,
                        multix=graphs, y_neg=(args.mpn_loss == 'margin'))
    vg = BatchGenerator((val_graphs, val_x) if graphs else train_x, val_y,
                        ids=data.df.iloc[data.val_indices].smiles.tolist(),
                        batch_size=args.batch_size, pair_step=args.pair_step,
                        pair_stop=args.pair_stop, use_weights=args.use_weights,
                        use_group_weights=(not args.no_group_weights),
                        dataset_info=data.df.dataset_id.iloc[data.val_indices].tolist(),
                        void_info=data.void_info, weight_steep=args.weight_steep,
                        no_inter_pairs=args.no_inter_pairs,
                        no_intra_pairs=args.no_intra_pairs,
                        max_indices_size=args.max_pair_compounds,
                        weight_mid=args.weight_mid,
                        multix=graphs, y_neg=(args.mpn_loss == 'margin'))
    if (args.plot_weights):
        plot_x = np.linspace(0, 10 * args.weight_mid, 100)
        import matplotlib.pyplot as plt
        plt.plot(plot_x, [bg.weight_fn(_, args.weight_steep, args.weight_mid) for _ in plot_x])
        plt.show()
    if (not graphs):
        if (ranker is None):    # otherwise loaded already
            ranker = prepare_tf_model(args, train_x.shape[1])
        es = tf.keras.callbacks.EarlyStopping(monitor='val_loss', patience=2,
                                              restore_best_weights=True)
        try:
            ranker.model.fit(bg,
                             callbacks=[es,
                                        # tf.keras.callbacks.TensorBoard(update_freq='epoch', histogram_freq=1,)
                                ],
                             epochs=args.epochs,
                             verbose=1 if not args.no_progbar else 2,
                             validation_data=vg)
        except KeyboardInterrupt:
            print('interrupted training, evaluating...')
        if (args.save_data):
            path = run_name + '.tf'
            ranker.model.save(path, overwrite=True)
            pickle.dump(data, open(os.path.join(path, 'assets', 'data.pkl'), 'wb'))
            json.dump({'train_sets': args.input, 'name': run_name,
                       'args': vars(args)},
                      open(os.path.join(path, 'assets', 'config.json'), 'w'), indent=2)
            print(f'model written to {path}')
        train_preds = predict(train_x, ranker.model, args.batch_size)
        if (len(val_x) > 0):
            val_preds = predict(val_x, ranker.model, args.batch_size)
        if (len(test_x) > 0):
            test_preds = predict(test_x, ranker.model, args.batch_size)
    else:
        # MPNranker
        if (ranker is None):    # otherwise loaded already
            ranker = MPNranker(sigmoid=(args.mpn_loss == 'bce'), extra_features_dim=train_x.shape[1],
                               hidden_units=args.sizes, encoder_size=args.encoder_size,
                               dropout_rate=args.dropout_rate)
        writer = SummaryWriter(f'runs/{run_name}_train')
        val_writer = SummaryWriter(f'runs/{run_name}_val')
        if (args.save_data):
            pickle.dump(data, open(os.path.join(f'{run_name}_data.pkl'), 'wb'))
            json.dump({'train_sets': args.input, 'name': run_name,
                       'args': args._log_all()},
                      open(f'{run_name}_config.json', 'w'), indent=2)
        try:
            mpn_train(ranker, bg, args.epochs, writer, vg, val_writer=val_writer,
                      steps_train_loss=np.ceil(len(bg) / 100).astype(int),
                      steps_val_loss=np.ceil(len(bg) / 5).astype(int),
                      batch_size=args.batch_size, epsilon=args.epsilon,
                      sigmoid_loss=(args.mpn_loss == 'bce'), margin_loss=args.mpn_margin,
                      early_stopping_patience=args.early_stopping_patience,
                      learning_rate=args.learning_rate, no_encoder_train=args.no_encoder_train,
                      ep_save=args.ep_save)
        except KeyboardInterrupt:
            print('caught interrupt; stopping training')
        if (args.save_data):
            torch.save(ranker, run_name + '.pt')
        train_preds = mpn_predict((train_graphs, train_x), ranker, batch_size=args.batch_size)
        if (len(val_x) > 0):
            val_preds = mpn_predict((val_graphs, val_x), ranker, batch_size=args.batch_size)
        if (len(test_x) > 0):
            test_preds = mpn_predict((test_graphs, test_x), ranker, batch_size=args.batch_size)
    print(f'train: {eval_(train_y, train_preds, args.epsilon):.3f}')
    if (len(test_x) > 0):
        print(f'test: {eval_(test_y, test_preds, args.epsilon):.3f}')
    if (len(val_x) > 0):
        print(f'val: {eval_(val_y, val_preds, args.epsilon):.3f}')
    if (args.export_rois and len(test_x) > 0):
        if not os.path.isdir('runs'):
            os.mkdir('runs')
        export_predictions(data, test_preds, f'runs/{run_name}_test.tsv', 'test')
    if (args.cache_file is not None and features.write_cache):
        print('writing cache, don\'t interrupt!!')
        pickle.dump(features.cached, open(args.cache_file, 'wb'))
