from itertools import combinations
from logging import basicConfig, INFO, info, warning
import pandas as pd
import numpy as np
from PIL import Image
from rdkit.Chem.Draw import MolToImage
from rdkit.Chem import MolFromSmiles
from keras import backend as K
import argparse
import os
import tensorflow as tf
import pickle
import json
import re
from tap import Tap
from typing import List, Optional, Literal, Tuple, Union
from tqdm import tqdm

import torch

from utils import REL_COLUMNS, Data, export_predictions
from features import features, parse_feature_spec

def eval_(y, preds, epsilon=1):
    assert len(y) == len(preds)
    if (not any(preds)):
        return 0.0
    preds, y = zip(*sorted(zip(preds, y)))
    matches = 0
    total = 0
    for i, j in combinations(range(len(y)), 2):
        diff = y[i] - y[j]
        if (diff < epsilon):
            matches += 1
        total += 1
    return matches / total if not total == 0 else np.nan

def eval2(df, epsilon=1, classyfire_level=None):
    df_eval = df.dropna(subset=['rt', 'roi'])
    df_eval.reset_index(drop=True, inplace=True)
    classes = (list(set(df_eval[classyfire_level].dropna().tolist()))
               if classyfire_level is not None else []) + ['total']
    matches = {c: [0 for i in range(len(df_eval))] for c in classes}
    total = {c: 0 for c in classes}
    for i, j in combinations(range(len(df_eval)), 2):
        rt_diff = df_eval.rt[i] - df_eval.rt[j]
        for c in classes:
            if (c != 'total' and
                df_eval[classyfire_level][i] == c or df_eval[classyfire_level][j] == c):
                match = 0
            else:
                match = ((np.sign(rt_diff) == np.sign(df_eval.roi[i] - df_eval.roi[j]))
                         or (np.abs(rt_diff) < epsilon)).astype(int)
                total[c] += 2
            matches[c][i] += match
            matches[c][j] += match
    df_eval['matches'] = matches['total']
    df_eval['matches_perc'] = df_eval.matches / total['total']
    df_classes = pd.DataFrame({'matches': matches})
    return (df_eval.matches.sum() / total['total'], df_eval,
            {'matches': {c: np.sum(matches[c]) for c in classes},
             'matches_perc': {c: np.sum(matches[c]) / total[c] for c in classes}})

def rt_roi_diffs(data, y, preds, k=3):
    """for all pairs x, y:
    is |rt_x - rt_y| very different from |roi_x - roi_y|?
    - increment outl[x], outl[y]
    - at the end return k u_i's with highest outl[u_i]
    """
    from pygam import LinearGAM
    assert len(y) == len(preds)
    scale_roi = max(preds) - min(preds)
    scale_rt = max(y) - min(y)
    df = pd.DataFrame(data.df.iloc[np.concatenate((data.train_indices, data.test_indices, data.val_indices))])
    df['roi'] = preds
    df.dropna(subset=['roi', 'rt'], inplace=True)
    df.sort_values(by='rt', inplace=True)
    # diffs = np.zeros((len(df)))
    # for i, j in combinations(range(len(y)), 2):
    #     diff_roi = np.abs(preds[i] - preds[j]) * scale_roi
    #     diff_rt = np.abs(y[i] - y[j]) * scale_rt
    #     diffs[i] += np.abs(diff_roi - diff_rt) / (len(y) ** 2)
    #     diffs[j] += np.abs(diff_roi - diff_rt) / (len(y) ** 2)
    # for i in range(k, len(df) - k):
    #     window = np.concatenate((df.roi[i-k:i], df.roi[i+1:i+k+1]))
    #     roi_mean = np.mean(window)
    #     diffs[i] = np.abs(df.roi[i] - roi_mean)
    gam = LinearGAM().fit(df.rt, df.roi)
    df['diffs'] = np.abs(df.roi - gam.predict(df.rt))
    df['rt_gam'] = LinearGAM().fit(df.roi, df.rt).predict(df.roi)
    df['diffs'] = (df['diffs'] > 0.2 * (np.sum(np.abs([min(df.roi), max(df.roi)])))).astype(int)
    return df

def visualize_df(df, x_axis='rt'):
    import matplotlib.pyplot as plt
    from matplotlib.offsetbox import OffsetImage, AnnotationBbox
    fig = plt.figure()
    ax = fig.add_subplot(111)
    x, y = ('rt', 'roi') if x_axis == 'rt' else ('roi', 'rt')
    points = ax.scatter(df[x], df[y], c=df.diffs, cmap='coolwarm')
    ax.set_xlabel(x)
    ax.set_ylabel(y)
    ax.set_title(df.id[0].split('_')[0])

    # blank image
    imm = Image.new('RGBA', (300, 300))
    im = OffsetImage(np.array(imm), zoom=0.5)
    xybox=(100., 100.)
    ab = AnnotationBbox(im, (0,0), xybox=xybox, xycoords='data',
            boxcoords="offset points",  pad=0.3,  arrowprops=dict(arrowstyle="->"))
    # add it to the axes and make it invisible
    ax.add_artist(ab)
    ab.set_visible(False)

    def recolor(df, ind, points, k=5):
        # find k points with closest rt
        rts = df.rt.values
        rt = rts[ind]
        rt_inds = np.argsort(np.abs(rts - rt))[:k]
        # find k points with closest roi
        rois = df.roi.values
        roi = rois[ind]
        roi_inds = np.argsort(np.abs(rois - roi))[:k]
        cols = {(True, False): [0, 0, 1, 1],
                (False, True): [1, 1, 0, 1],
                (True, True): [0, 1, 0, 1],
                (False, False): [0, 0, 0, 1]}
        colors = [cols[(p in rt_inds, p in roi_inds)] for p in range(len(rts))]
        return colors


    def hover(event):
        if (not hasattr(points, 'def_colors')):
            points.def_colors = points.get_facecolors()
        # if the mouse is over the scatter points
        if points.contains(event)[0]:
            # find out the index within the array from the event
            ind = points.contains(event)[1]["ind"][0]
            points.set_facecolors(recolor(df, ind, points))
            # get the figure size
            w,h = fig.get_size_inches()*fig.dpi
            ws = (event.x > w/2.)*-1 + (event.x <= w/2.)
            hs = (event.y > h/2.)*-1 + (event.y <= h/2.)
            # if event occurs in the top or right quadrant of the figure,
            # change the annotation box position relative to mouse.
            ab.xybox = (xybox[0]*ws, xybox[1]*hs)
            # make annotation box visible
            ab.set_visible(True)
            # place it at the position of the hovered scatter point
            ab.xy =(df.rt.iloc[ind], df.roi.iloc[ind])
            # set the image corresponding to that point
            im.set_data(np.array(MolToImage(MolFromSmiles(df.smiles.iloc[ind]), (300, 300))))
        else:
            #if the mouse is not over a scatter point
            ab.set_visible(False)
            points.set_facecolors(points.def_colors)
        fig.canvas.draw_idle()

    fig.canvas.mpl_connect('motion_notify_event', hover)
    plt.show()


def data_stats(d, data, custom_column_fields=None):
    train_compounds_all = set(data.df['inchi.std'])
    this_column = d.df['column.name'].values[0]
    train_compounds_col = set(data.df.loc[data.df['column.name'] == this_column, 'inchi.std'])
    test_compounds = set(d.df['inchi.std'])
    system_fields = custom_column_fields if custom_column_fields is not None else REL_COLUMNS
    train_configs = [t[1:] for t in set(data.df[['dataset_id', 'column.name'] + system_fields]
                                        .itertuples(index=False, name=None))]
    test_config = tuple(d.df[['column.name'] + system_fields].iloc[0].tolist())
    same_config = len([t for t in train_configs if t == test_config])
    same_column = len([t for t in train_configs if t[0] == test_config[0]])
    return {'num_data': len(test_compounds),
            'compound_overlap_all': (len(test_compounds & train_compounds_all)
                                           / len(test_compounds)),
            'compound_overlap_column': (len(test_compounds & train_compounds_col)
                                              / len(test_compounds)),
            'column_occurences': same_column,
            'config_occurences': same_config}


def predict(X, model, batch_size):
    preds = []
    ranker_output = K.function([model.layers[0].input], [model.layers[-3].get_output_at(0)])
    for x in np.array_split(X, np.ceil(X.shape[0] / batch_size * 10)):
        preds.append(ranker_output([x])[0].ravel())
    return np.concatenate(preds)

class EvalArgs(Tap):
    model: str # model to load
    test_sets: List[str] # either CSV or dataset IDs to evaluate on
    model_type: Literal['ranknet', 'mpn'] = 'ranknet'
    batch_size: int = 256
    isomeric: bool = False
    repo_root_folder: str = '/home/fleming/Documents/Projects/RtPredTrainingData/' # location of the dataset github repository
    add_desc_file: str = '/home/fleming/Documents/Projects/rtranknet/data/qm_merged.csv' # csv with additional features with smiles as identifier
    verbose: bool = False
    no_progbar: bool = False # no progress-bar
    void_rt: float = 0.0
    metadata_void_rt: bool = False # use t0 value from repo metadata (times 3)
    cache_file: str = 'cached_descs.pkl'
    export_rois: bool = False
    device: Optional[str] = None # can be `mirrored`, a specific device name like `gpu:1` or `None` which automatically selects an option
    epsilon: float = 0.5 # difference in evaluation measure below which to ignore falsely predicted pairs
    remove_train_compounds: bool = False
    remove_train_compounds_mode: Literal['all', 'column', 'print'] = 'all'
    plot_diffs: bool = False    # plot for every dataset with outliers marked
    test_stats: bool = False    # overview stats for all datasets
    dataset_stats: bool = False # stats for each dataset
    diffs: bool = False         # compute outliers
    classyfire: bool = False    # compound class stats

def load_model(path: str, type_='keras'):
    if (type_ == 'keras'):
        model = tf.keras.models.load_model(path)
        data = pickle.load(open(os.path.join(path, 'assets', 'data.pkl'), 'rb'))
        config = json.load(open(os.path.join(path, 'assets', 'config.json')))
    else:
        if (torch.cuda.is_available()):
            model = torch.load(path + '.pt')
        else:
            model = torch.load(path + '.pt', map_location=torch.device('cpu'))
            model.encoder.device = torch.device('cpu')
        data = pickle.load(open(f'{path}_data.pkl', 'rb'))
        config = json.load(open(f'{path}_config.json'))
    return model, data, config


def classyfire_stats(d: Data, args: EvalArgs, plot=False):
    acc2, results, matches = eval2(d.df, args.epsilon, 'classyfire.class')
    print(f'{ds}: {acc2:.2%} accuracy)')
    groups = results.groupby('classyfire.class')
    results['matches_perc'] = results.matches_perc * len(results)
    # print(groups.matches_perc.agg(['mean', 'median', 'std', 'count']))
    # print(results.groupby('classyfire.class').matches_perc.agg(['mean', 'median', 'std', 'count']))
    matches_df = pd.DataFrame.from_dict(matches['matches_perc'], orient='index', columns=['acc_without'])
    matches_df['acc_without_diff'] = matches_df.acc_without - acc2
    matches_df['num_compounds'] = ([len(d.df.loc[d.df['classyfire.class'] == c])
                                    for c in matches_df.index.tolist()[:-1]]
                                   + [len(d.df)])
    matches_df['class_perc'] = matches_df.num_compounds / len(d.df)
    train_compounds = []
    train_compounds_all = len(set(data.df['inchi.std'].tolist()))
    for c in matches_df.index.tolist()[:-1]:
        compounds_perc = len(set(data.df.loc[data.df['classyfire.class'] == c,
                                        'inchi.std'].tolist())) / train_compounds_all
        train_compounds.append(compounds_perc)
    matches_df['class_perc_train'] = train_compounds + [1.0]
    matches_df.index = [re.sub(r' \(CHEMONTID:\d+\)', '', i) for i in matches_df.index]
    print(matches_df.sort_values(by='acc_without_diff', ascending=False)[
        ['acc_without_diff', 'num_compounds', 'class_perc', 'class_perc_train']])
    if (plot):       # plotting
        matches_df.drop('total').sort_values(by='acc_without_diff', ascending=False)[
            ['acc_without_diff', 'class_perc', 'class_perc_train']].plot(rot=20)
        import matplotlib.pyplot as plt
        plt.tight_layout()
        plt.show()

def compound_acc(y, preds, comp_index, epsilon=0.5):
    matches = 0
    total = 0
    for j in range(len(y)):
        diff = (y[comp_index] - y[j]) * - np.sign(preds[comp_index] - preds[j])
        if (diff < epsilon):
            matches += 1
        total += 1
    return matches / total

def compound_stats(d: Data, args:EvalArgs):
    # logp
    from rdkit.Chem.Descriptors import MolLogP
    d.df['MolLogP'] = [MolLogP(MolFromSmiles(s)) for s in d.df.smiles]
    # mean compound acc
    d.df['mean_acc'] = [compound_acc(d.df.rt.tolist(), d.df.roi.tolist(), i, epsilon=args.epsilon)
                        for i in range(len(d.df))]

def density_plot(df: pd.DataFrame, x, y):
    from scipy.stats import gaussian_kde
    import matplotlib.pyplot as plt
    toplot = df.sort_values(by=x)[[x, y]].rolling(len(df) / 100).mean().dropna()
    xy = np.vstack([toplot[x], toplot[y]])
    z = gaussian_kde(xy)(xy)
    toplot.plot.scatter(x, y, c=z)
    plt.show()

def pair_stats(d: Data, verbose=False):
    fields = {}
    it = combinations(range(len(d.df)), 2)
    if (verbose):
        it = tqdm(it)
    for i, j in it:
        row_i, row_j = d.df.iloc[i], d.df.iloc[j]
        fields.setdefault('indices', []).append((i, j))
        fields.setdefault('abs_rt_diff', []).append(np.abs(row_i.rt - row_j.rt))
        fields.setdefault('abs_roi_diff', []).append(np.abs(row_i.roi - row_j.roi))
        fields.setdefault('prediction_correct', []).append(np.sign(row_i.rt - row_j.rt) == np.sign(row_i.roi - row_j.roi))
        if ('MolLogP' in d.df.columns):
            fields.setdefault('MolLogP_diff', []).append(np.abs(row_i.MolLogP - row_j.MolLogP))
    return pd.DataFrame(fields)

if __name__ == '__main__':
    if '__file__' in globals():
        args = EvalArgs().parse_args()
    else:
        args = EvalArgs().parse_args('hsm_new.tf 0004 --test_stats'.split())

    if (args.verbose):
        basicConfig(level=INFO)

    # load model
    info('load model...')
    model, data, config = load_model(args.model, args.model_type)
    features_type = parse_feature_spec(config['args']['feature_type'])['mode']
    features_add = config['args']['add_descs']
    n_thr = config['args']['num_features']
    info('load cache')
    # load cached descriptors
    if (args.cache_file is not None):
        features.write_cache = False # flag for reporting changes to cache
        info('load cache')
        if (os.path.exists(args.cache_file)):
            features.cached = pickle.load(open(args.cache_file, 'rb'))
        else:
            features.cached = {}
            warning('cache file does not exist yet')

    test_stats = []
    data_args = {'use_compound_classes': data.use_compound_classes,
                 'use_system_information': data.use_system_information,
                 'metadata_void_rt': args.metadata_void_rt,
                 'classes_l_thr': data.classes_l_thr,
                 'classes_u_thr': data.classes_u_thr,
                 'use_usp_codes': data.use_usp_codes,
                 'custom_features': data.descriptors,
                 'use_hsm': data.use_hsm,
                 'repo_root_folder': args.repo_root_folder,
                 'custom_column_fields': data.custom_column_fields,
                 'columns_remove_na': False,
                 'hsm_fields': data.hsm_fields,
                 'graph_mode': args.model_type == 'mpn'}
    info('model preprocessing done')
    for ds in args.test_sets:
        info(f'loading data for {ds}')
        if (not re.match(r'\d{4}', ds)):
            # raw file
            d = Data.from_raw_file(ds, void_rt=args.void_rt, **data_args)
            d.custom_features = data.descriptors
        else:
            d = Data(**data_args)
            d.add_dataset_id(ds,
                             repo_root_folder=args.repo_root_folder,
                             void_rt=args.void_rt,
                             isomeric=args.isomeric)
        if (args.remove_train_compounds):
            info('removing train compounds')
            train_compounds_all = set(data.df['inchi.std'])
            this_column = d.df['column.name'].values[0]
            train_compounds_col = set(data.df.loc[data.df['column.name'] == this_column, 'inchi.std'])
            if (args.remove_train_compounds_mode == 'print'):
                print('compounds overlap to training data: '
                      + f'{len(set(d.df["inchi.std"]) & train_compounds_all) / len(set(d.df["inchi.std"])) * 100:.0f}% (all), '
                      + f'{len(set(d.df["inchi.std"]) & train_compounds_col) / len(set(d.df["inchi.std"])) * 100:.0f}% (same column)')
            else:
                if (args.remove_train_compounds_mode == 'all'):
                    train_compounds = train_compounds_all
                elif (args.remove_train_compounds_mode == 'column'):
                    train_compounds = train_compounds_col
                else:
                    raise NotImplementedError(args.remove_train_compounds_mode)
                prev_len = len(d.df)
                d.df = d.df.loc[~d.df['inchi.std'].isin(train_compounds)]
                if args.verbose:
                    print(f'{ds} evaluation: removed {prev_len - len(d.df)} compounds also appearing '
                          f'in the training data (now {len(d.df)} compounds)')
        if (len(d.df) < 2):
            print(f'too few compounds ({len(d.df)}), skipping ...')
            continue
        info('computing features')
        d.compute_features(verbose=args.verbose, mode=features_type, add_descs=features_add,
                           add_desc_file=args.add_desc_file, n_thr=n_thr)
        if (args.model_type == 'mpn'):
            info('computing graphs')
            d.compute_graphs()
        info('(fake) splitting data')
        d.split_data((0, 0))
        if (hasattr(data, 'scaler')):
            info('standardize data')
            d.standardize(data.scaler)
        ((train_graphs, train_x, train_y),
         (val_graphs, val_x, val_y),
         (test_graphs, test_x, test_y)) = d.get_split_data()
        X = np.concatenate((train_x, test_x, val_x))
        Y = np.concatenate((train_y, test_y, val_y))
        info('done preprocessing. predicting...')
        if (args.model_type == 'mpn'):
            from mpnranker import predict as mpn_predict
            graphs = np.concatenate((train_graphs, test_graphs, val_graphs))
            preds = mpn_predict((graphs, X), model, batch_size=args.batch_size,
                                prog_bar=args.verbose)
        else:
            preds = predict(X, model, args.batch_size)
        info('done predicting. evaluation...')
        acc = eval_(Y, preds, args.epsilon)
        d.df['roi'] = preds[np.arange(len(d.df.rt))[ # restore correct order
            np.argsort(np.concatenate([d.train_indices, d.test_indices, d.val_indices]))]]
        # acc2, results = eval2(d.df, args.epsilon)
        if (args.classyfire):
            info('computing classyfire stats')
            classyfire_stats(d, args)
        if (args.dataset_stats):
            info('computing dataset stats')
            dataset_stats(d)
            pass
        if (args.test_stats):
            info('computing test stats')
            stats = data_stats(d, data, data.custom_column_fields)
            stats.update({'acc': acc, 'id': ds})
            test_stats.append(stats)
        else:
            print(f'{ds}: {acc:.3f} \t (#data: {len(Y)})')
        if (args.diffs):
            info('computing outlier stats')
            df = rt_roi_diffs(d, Y, preds)
            # with pd.option_context('display.max_rows', None):
            #     print(df.sort_values(by='rt')[['id', 'rt', 'roi', 'diffs']])
            print('outliers:')
            print(df.loc[df.diffs == 1, ['id', 'roi', 'rt', 'rt_gam']])
            if (args.plot_diffs):
                visualize_df(df)
        if (args.export_rois):
            info('exporting ROIs')
            # TODO: don't overwrite
            if (not re.match(r'\d{4}', ds)):
                ds = os.path.basename(ds)
            export_predictions(d, preds, f'runs/{config["name"]}_{ds}.tsv')
        if (False and args.classyfire):
            fig = px.treemap(d.df.dropna(subset=['classyfire.kingdom', 'classyfire.superclass', 'classyfire.class']),
                             path=['classyfire.kingdom', 'classyfire.superclass', 'classyfire.class'],
                             title=f'{ds} data ({acc:.2%} accuracy)')
            fig.show(renderer='browser')
    if (args.test_stats and len(test_stats) > 0):
        print(pd.DataFrame.from_records(test_stats, index='id'))
