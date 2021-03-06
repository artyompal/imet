#!/usr/bin/python3.6
''' Trains a model or infers predictions. '''

import argparse
import math
import os
import pprint
import random
import re
import sys
import time
import yaml

from typing import *
from collections import defaultdict, Counter
from glob import glob

import numpy as np
import pandas as pd

import torch
import torch.nn as nn
import torch.backends.cudnn as cudnn
import torch.nn.functional as F

from torch.utils.data import TensorDataset
from sklearn.preprocessing import LabelEncoder
from tqdm import tqdm
from easydict import EasyDict as edict
from scipy.stats import describe

import albumentations as albu

from utils import create_logger, AverageMeter
from debug import dprint

from parse_config import load_config
from losses import get_loss
from schedulers import get_scheduler, is_scheduler_continuous, get_warmup_scheduler
from optimizers import get_optimizer, get_lr, set_lr
from metrics import F_score
from random_rect_crop import RandomRectCrop
from random_erase import RandomErase
from model import freeze_layers, unfreeze_layers
from cosine_scheduler import CosineLRWithRestarts
from torch.optim.lr_scheduler import ReduceLROnPlateau

IN_KERNEL = os.environ.get('KAGGLE_WORKING_DIR') is not None
INPUT_PATH = '../input/imet-2019-fgvc6/' if IN_KERNEL else '../input/'
ADDITIONAL_DATASET_PATH = '../input/imet-datasets/'
CONFIG_PATH = 'config/' if not IN_KERNEL else '../input/imet-yaml/yml/'

if not IN_KERNEL:
    import torchsummary


def find_input_file(path: str) -> str:
    path = INPUT_PATH + os.path.basename(path)
    return path if os.path.exists(path) else ADDITIONAL_DATASET_PATH + os.path.basename(path)

def train_val_split(df: pd.DataFrame, fold: int) -> Tuple[pd.DataFrame, pd.DataFrame]:
    folds = np.load(config.train.folds_file)
    assert folds.shape[0] == df.shape[0]
    return df.loc[folds != fold], df.loc[folds == fold]

def parse_labels(s: str) -> np.array:
    res = np.zeros(config.model.num_classes)
    res[list(map(int, s.split()))] = 1
    return res

def load_data(fold: int) -> Any:
    torch.multiprocessing.set_sharing_strategy('file_system') # type: ignore
    cudnn.benchmark = True # type: ignore

    logger.info('config:')
    logger.info(pprint.pformat(config))

    fold_num = np.load('folds.npy')
    train_df = pd.read_csv(INPUT_PATH + 'train.csv')

    # TODO: load the test set
    # test_df = pd.read_csv(find_input_file(INPUT_PATH + 'sample_submission.csv'))

    all_targets = np.vstack(list(map(parse_labels, train_df.attribute_ids)))

    # build dataset
    all_predicts_list, all_thresholds = [], []
    predicts = sorted(sys.argv[1:])
    logger.info('loading data')

    for model_files in tqdm(config.data.inputs, disable=IN_KERNEL):
        predict = np.zeros((train_df.shape[0], config.model.num_classes))

        for fold in range(config.model.num_folds):
            # load data
            filename = model_files[fold]
            data = np.load(os.path.join(config.data.input_dir, filename))

            # read threshold
            filename = os.path.basename(filename)
            assert filename.startswith('level1_train_') and filename.endswith('.npy')

            with open(os.path.join('../yml/', filename[13:-4] + '.yml')) as f:
                threshold = yaml.load(f, Loader=yaml.SafeLoader)['threshold']
                all_thresholds.append(threshold)
                data = data + threshold

            if np.min(data) < 0 or np.max(data) > 1:
                print('invalid range of data:', describe(data))

            predict[fold_num == fold] = data

        all_predicts_list.append(predict)

    all_predicts = np.dstack(all_predicts_list)
    dprint(all_predicts.shape)
    dprint(all_targets.shape)

    x_train = torch.tensor(all_predicts[fold_num != args.fold], dtype=torch.float32)
    x_train = x_train.view(x_train.shape[0], -1, 1)
    y_train = torch.tensor(all_targets[fold_num != args.fold], dtype=torch.float32)

    x_val = torch.tensor(all_predicts[fold_num == args.fold], dtype=torch.float32)
    x_val = x_val.view(x_val.shape[0], -1, 1)
    y_val = torch.tensor(all_targets[fold_num == args.fold], dtype=torch.float32)

    dprint(x_train.shape)
    dprint(y_train.shape)
    dprint(x_val.shape)
    dprint(y_val.shape)

    train_dataset = TensorDataset(x_train, y_train)
    val_dataset = TensorDataset(x_val, y_val)

    train_loader = torch.utils.data.DataLoader(
        train_dataset, batch_size=config.train.batch_size, shuffle=True,
        num_workers=config.num_workers, drop_last=True)

    val_loader = torch.utils.data.DataLoader(
        val_dataset, batch_size=config.train.batch_size, shuffle=False,
        num_workers=config.num_workers)

    return train_loader, val_loader, None

def lr_finder(train_loader: Any, model: Any, criterion: Any, optimizer: Any) -> None:
    ''' Finds the optimal LR range and sets up first optimizer parameters. '''
    logger.info('lr_finder called')

    batch_time = AverageMeter()
    num_steps = min(len(train_loader), config.train.lr_finder.num_steps)
    logger.info(f'total batches: {num_steps}')
    end = time.time()
    lr_str = ''
    model.train()

    init_value = config.train.lr_finder.init_value
    final_value = config.train.lr_finder.final_value
    beta = config.train.lr_finder.beta

    mult = (final_value / init_value) ** (1 / (num_steps - 1))
    lr = init_value

    avg_loss = best_loss = 0.0
    losses = np.zeros(num_steps)
    logs = np.zeros(num_steps)

    for i, (input_, target) in enumerate(train_loader):
        if i >= num_steps:
            break

        set_lr(optimizer, lr)

        output = model(input_.cuda())
        loss = criterion(output, target.cuda())
        loss_val = loss.data.item()

        predict = (output.detach() > 0.1).type(torch.FloatTensor)
        f2 = F_score(predict, target, beta=2)

        optimizer.zero_grad()
        loss.backward()
        optimizer.step()

        lr_str = f'\tlr {lr:.08f}'

        # compute the smoothed loss
        avg_loss = beta * avg_loss + (1 - beta) * loss_val
        smoothed_loss = avg_loss / (1 - beta ** (i + 1))

        # stop if the loss is exploding
        if i > 0 and smoothed_loss > 4 * best_loss:
            break

        # record the best loss
        if smoothed_loss < best_loss or i == 0:
            best_loss = smoothed_loss

        # store the values
        losses[i] = smoothed_loss
        logs[i] = math.log10(lr)

        # update the lr for the next step
        lr *= mult

        batch_time.update(time.time() - end)
        end = time.time()

        if i % config.train.log_freq == 0:
            logger.info(f'lr_finder [{i}/{num_steps}]\t'
                        f'time {batch_time.val:.3f} ({batch_time.avg:.3f})\t'
                        f'loss {loss:.4f} ({smoothed_loss:.4f})\t'
                        f'F2 {f2:.4f} {lr_str}')

    np.savez(os.path.join(config.experiment_dir, f'lr_finder_{config.version}'),
             logs=logs, losses=losses)

    d1 = np.zeros_like(losses); d1[1:] = losses[1:] - losses[:-1]
    first, last = np.argmin(d1), np.argmin(losses)

    MAGIC_COEFF = 4

    highest_lr = 10 ** logs[last]
    best_high_lr = highest_lr / MAGIC_COEFF
    best_low_lr = 10 ** logs[first]
    logger.info(f'best_low_lr={best_low_lr} best_high_lr={best_high_lr} '
                f'highest_lr={highest_lr}')

    def find_nearest(array: np.array, value: float) -> int:
        return (np.abs(array - value)).argmin()

    last = find_nearest(logs, math.log10(best_high_lr))
    logger.info(f'first={first} last={last}')

    import matplotlib.pyplot as plt
    plt.plot(logs, losses, '-D', markevery=[first, last])
    plt.savefig(os.path.join(config.experiment_dir, 'lr_finder_plot.png'))

def mixup(x: torch.Tensor, y: torch.Tensor) -> Tuple[torch.Tensor, torch.Tensor]:
    ''' Performs mixup: https://arxiv.org/pdf/1710.09412.pdf '''
    coeff = np.random.beta(config.train.mixup.beta_a, config.train.mixup.beta_a)
    indices = np.roll(np.arange(x.shape[0]), np.random.randint(1, x.shape[0]))
    indices = torch.tensor(indices).cuda()

    x = x * coeff + x[indices] * (1 - coeff)
    y = y * coeff + y[indices] * (1 - coeff)
    return x, y

def train_epoch(train_loader: Any, model: Any, criterion: Any, optimizer: Any,
                epoch: int, lr_scheduler: Any, lr_scheduler2: Any,
                max_steps: Optional[int]) -> None:
    logger.info(f'epoch: {epoch}')
    logger.info(f'learning rate: {get_lr(optimizer)}')

    batch_time = AverageMeter()
    losses = AverageMeter()
    avg_score = AverageMeter()

    model.train()
    optimizer.zero_grad()

    num_steps = len(train_loader)
    if max_steps:
        num_steps = min(max_steps, num_steps)
    num_steps -= num_steps % config.train.accum_batches_num

    logger.info(f'total batches: {num_steps}')
    end = time.time()
    lr_str = ''

    for i, (input_, target) in enumerate(train_loader):
        if i >= num_steps:
            break

        if config.train.mixup.enable:
            input_, target = mixup(input_, target)

        output = model(input_)
        loss = criterion(output, target)

        predict = (output.detach() > 0.1).type(torch.FloatTensor)
        avg_score.update(F_score(predict, target, beta=2))

        losses.update(loss.data.item(), input_.size(0))
        loss.backward()

        if (i + 1) % config.train.accum_batches_num == 0:
            optimizer.step()
            optimizer.zero_grad()

        if is_scheduler_continuous(lr_scheduler):
            lr_scheduler.step()
            lr_str = f'\tlr {get_lr(optimizer):.02e}'
        elif is_scheduler_continuous(lr_scheduler2):
            lr_scheduler2.step()
            lr_str = f'\tlr {get_lr(optimizer):.08f}'

        batch_time.update(time.time() - end)
        end = time.time()

        if i % config.train.log_freq == 0:
            logger.info(f'{epoch} [{i}/{num_steps}]\t'
                        f'time {batch_time.val:.3f} ({batch_time.avg:.3f})\t'
                        f'loss {losses.val:.4f} ({losses.avg:.4f})\t'
                        f'F2 {avg_score.val:.4f} ({avg_score.avg:.4f})'
                        + lr_str)

    logger.info(f' * average F2 on train {avg_score.avg:.4f}')

def inference(data_loader: Any, model: Any) -> Tuple[torch.Tensor, Optional[torch.Tensor]]:
    ''' Returns predictions and targets, if any. '''
    model.eval()
    predicts_list, targets_list = [], []

    with torch.no_grad():
        for input_data in tqdm(data_loader, disable=IN_KERNEL):
            # if data_loader.dataset.mode != 'test':
            input_, target = input_data
            # else:
            #     input_, target = input_data, None
            #
            # if data_loader.dataset.num_ttas != 1:
            #     bs, ncrops, c, h, w = input_.size()
            #     input_ = input_.view(-1, c, h, w) # fuse batch size and ncrops
            #
            #     output = model(input_)
            #
            #     if config.test.tta_combine_func == 'max':
            #         output = output.view(bs, ncrops, -1).max(1)[0]
            #     elif config.test.tta_combine_func == 'mean':
            #         output = output.view(bs, ncrops, -1).mean(1)
            #     else:
            #         assert False
            # else:
            output = model(input_)

            predicts_list.append(output.detach().cpu().numpy())
            if target is not None:
                targets_list.append(target)

    predicts = np.concatenate(predicts_list)
    targets = np.concatenate(targets_list) if targets_list else None
    return predicts, targets

def validate(val_loader: Any, model: Any, epoch: int) -> Tuple[float, float, np.ndarray]:
    ''' Calculates validation score.
    1. Infers predictions
    2. Finds optimal threshold
    3. Returns the best score and a threshold. '''
    logger.info('validate()')

    predicts, targets = inference(val_loader, model)
    predicts, targets = torch.tensor(predicts), torch.tensor(targets)
    best_score, best_thresh = 0.0, 0.0

    for threshold in tqdm(np.linspace(0.05, 0.25, 100), disable=IN_KERNEL):
        score = F_score(predicts, targets, beta=2, threshold=threshold)
        if score > best_score:
            best_score, best_thresh = score, threshold.item()

    logger.info(f'{epoch} F2 {best_score:.4f} threshold {best_thresh:.4f}')
    logger.info(f' * F2 on validation {best_score:.4f}')
    return best_score, best_thresh, predicts.numpy()

def gen_train_prediction(data_loader: Any, model: Any, epoch: int,
                         model_path: str) -> np.ndarray:
    score, threshold, predicts = validate(data_loader, model, epoch)
    predicts -= threshold

    filename = os.path.splitext(os.path.basename(model_path))[0]
    np.save(f'level1_train_{filename}.npy', predicts)

    with open(f'{filename}.yml', 'w') as f:
        yaml.dump({'threshold': threshold}, f)

def gen_test_prediction(data_loader: Any, model: Any, model_path: str) -> np.ndarray:
    threshold_file = threshold_files[os.path.splitext(os.path.basename(model_path))[0] + '.yml']

    with open(threshold_file) as f:
        threshold = yaml.load(f, Loader=yaml.SafeLoader)['threshold']

    predicts, _ = inference(data_loader, model)
    predicts -= threshold

    filename = f'level1_test_{os.path.splitext(os.path.basename(model_path))[0]}'
    np.save(filename, predicts)

class Model(nn.Module):
    def __init__(self, config: Any) -> None:
        super().__init__()
        self.layer = nn.Conv1d(
            in_channels=config.model.num_classes * len(config.data.inputs),
            out_channels=config.model.num_classes,
            kernel_size=1,
            groups=config.model.num_classes
            )

    def forward(self, x: torch.Tensor) -> torch.Tensor: # type: ignore
        x = self.layer(x)
        x = torch.clamp(x, 0, 1)
        x = x.view(x.shape[0], -1)
        return x

def run() -> float:
    np.random.seed(0)
    model_dir = config.experiment_dir

    logger.info('=' * 50)

    train_loader, val_loader, test_loader = load_data(args.fold)
    logger.info(f'creating a model {config.model.arch}')
    model = Model(config)
    criterion = get_loss(config)

    if args.summary:
        torchsummary.summary(model, (config.model.num_classes * len(config.data.inputs), 1))

    if args.lr_finder:
        optimizer = get_optimizer(config, model.parameters())
        lr_finder(train_loader, model, criterion, optimizer)
        sys.exit()

    if args.weights is None and config.train.head_only_warmup:
        logger.info('-' * 50)
        logger.info(f'doing warmup for {config.train.warmup.steps} steps')
        logger.info(f'max_lr will be {config.train.warmup.max_lr}')

        optimizer = get_optimizer(config, model.parameters())
        warmup_scheduler = get_warmup_scheduler(config, optimizer)

        freeze_layers(model)
        train_epoch(train_loader, model, criterion, optimizer, 0,
                    warmup_scheduler, None, config.train.warmup.steps)
        unfreeze_layers(model)

    if args.weights is None and config.train.enable_warmup:
        logger.info('-' * 50)
        logger.info(f'doing warmup for {config.train.warmup.steps} steps')
        logger.info(f'max_lr will be {config.train.warmup.max_lr}')

        optimizer = get_optimizer(config, model.parameters())
        warmup_scheduler = get_warmup_scheduler(config, optimizer)
        train_epoch(train_loader, model, criterion, optimizer, 0,
                    warmup_scheduler, None, config.train.warmup.steps)

    optimizer = get_optimizer(config, model.parameters())

    if args.weights is None:
        last_epoch = -1
    else:
        last_checkpoint = torch.load(args.weights)
        model_arch = last_checkpoint['arch'].replace('se_', 'se')

        if model_arch != config.model.arch:
            dprint(model_arch)
            dprint(config.model.arch)
            assert model_arch == config.model.arch

        model.load_state_dict(last_checkpoint['state_dict'])
        optimizer.load_state_dict(last_checkpoint['optimizer'])
        logger.info(f'checkpoint loaded: {args.weights}')

        last_epoch = last_checkpoint['epoch']
        logger.info(f'loaded the model from epoch {last_epoch}')

        if args.lr != 0:
            set_lr(optimizer, float(args.lr))
        elif 'lr' in config.optimizer.params:
            set_lr(optimizer, config.optimizer.params.lr)
        elif 'base_lr' in config.scheduler.params:
            set_lr(optimizer, config.scheduler.params.base_lr)

    if not args.cosine:
        lr_scheduler = get_scheduler(config.scheduler, optimizer, last_epoch=
                                     (last_epoch if config.scheduler.name != 'cyclic_lr' else -1))
        assert config.scheduler2.name == ''
        lr_scheduler2 = get_scheduler(config.scheduler2, optimizer, last_epoch=last_epoch) \
                        if config.scheduler2.name else None
    else:
        epoch_size = min(len(train_loader), config.train.max_steps_per_epoch) \
                     * config.train.batch_size

        set_lr(optimizer, float(config.cosine.start_lr))
        lr_scheduler = CosineLRWithRestarts(optimizer,
                                            batch_size=config.train.batch_size,
                                            epoch_size=epoch_size,
                                            restart_period=config.cosine.period,
                                            period_inc=config.cosine.period_inc,
                                            max_period=config.cosine.max_period)
        lr_scheduler2 = None

    if args.predict_oof or args.predict_test:
        print('inference mode')
        assert args.weights is not None

        if args.predict_oof:
            gen_train_prediction(val_loader, model, last_epoch, args.weights)
        else:
            gen_test_prediction(test_loader, model, args.weights)

        sys.exit()

    logger.info(f'training will start from epoch {last_epoch + 1}')

    best_score = 0.0
    best_epoch = 0

    last_lr = get_lr(optimizer)
    best_model_path = args.weights

    for epoch in range(last_epoch + 1, config.train.num_epochs):
        logger.info('-' * 50)

        if not is_scheduler_continuous(lr_scheduler) and lr_scheduler2 is None:
            # if we have just reduced LR, reload the best saved model
            lr = get_lr(optimizer)

            if lr < last_lr - 1e-10 and best_model_path is not None:
                logger.info(f'learning rate dropped: {lr}, reloading')
                last_checkpoint = torch.load(best_model_path)

                assert(last_checkpoint['arch']==config.model.arch)
                model.load_state_dict(last_checkpoint['state_dict'])
                optimizer.load_state_dict(last_checkpoint['optimizer'])
                logger.info(f'checkpoint loaded: {best_model_path}')
                set_lr(optimizer, lr)
                last_lr = lr

        if config.train.lr_decay_coeff != 0 and epoch in config.train.lr_decay_milestones:
            n_cycles = config.train.lr_decay_milestones.index(epoch) + 1
            total_coeff = config.train.lr_decay_coeff ** n_cycles
            logger.info(f'artificial LR scheduler: made {n_cycles} cycles, decreasing LR by {total_coeff}')

            set_lr(optimizer, config.scheduler.params.base_lr * total_coeff)
            lr_scheduler = get_scheduler(config.scheduler, optimizer,
                                         coeff=total_coeff, last_epoch=-1)
                                         # (last_epoch if config.scheduler.name != 'cyclic_lr' else -1))

        if isinstance(lr_scheduler, CosineLRWithRestarts):
            restart = lr_scheduler.epoch_step()
            if restart:
                logger.info('cosine annealing restarted, resetting the best metric')
                best_score = min(config.cosine.min_metric_val, best_score)

        train_epoch(train_loader, model, criterion, optimizer, epoch,
                    lr_scheduler, lr_scheduler2, config.train.max_steps_per_epoch)
        score, _, _ = validate(val_loader, model, epoch)

        if type(lr_scheduler) == ReduceLROnPlateau:
            lr_scheduler.step(metrics=score)
        elif not is_scheduler_continuous(lr_scheduler):
            lr_scheduler.step()

        if type(lr_scheduler2) == ReduceLROnPlateau:
            lr_scheduler2.step(metrics=score)
        elif lr_scheduler2 and not is_scheduler_continuous(lr_scheduler2):
            lr_scheduler2.step()

        is_best = score > best_score
        best_score = max(score, best_score)
        if is_best:
            best_epoch = epoch

        if is_best:
            best_model_path = os.path.join(model_dir,
                f'{config.version}_f{args.fold}_e{epoch:02d}_{score:.04f}.pth')

            data_to_save = {
                'epoch': epoch,
                'arch': config.model.arch,
                'state_dict': model.state_dict(),
                'score': score,
                'optimizer': optimizer.state_dict(),
                'config': config
            }

            torch.save(data_to_save, best_model_path)
            logger.info(f'a snapshot was saved to {best_model_path}')

    logger.info(f'best score: {best_score:.04f}')
    return -best_score

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', help='model configuration file (YAML)', type=str)
    parser.add_argument('--lr_finder', help='invoke LR finder and exit', action='store_true')
    parser.add_argument('--weights', help='model to resume training', type=str)
    parser.add_argument('--fold', help='fold number', type=int, default=0)
    parser.add_argument('--predict_oof', help='make predictions for the train set and return', action='store_true')
    parser.add_argument('--predict_test', help='make predictions for the testset and return', action='store_true')
    parser.add_argument('--summary', help='show model summary', action='store_true')
    parser.add_argument('--lr', help='override learning rate', type=float, default=0)
    parser.add_argument('--num_epochs', help='override number of epochs', type=int, default=0)
    parser.add_argument('--num_ttas', help='override number of TTAs', type=int, default=0)
    parser.add_argument('--cosine', help='enable cosine annealing', type=bool, default=False)
    args = parser.parse_args()

    if not args.config:
        if not args.weights:
            print('you must specify either --config or --weights')
            sys.exit()

        # f'{config.version}_f{args.fold}_e{epoch:02d}_{score:.04f}.pth')
        m = re.match(r'(.*)_f(\d)_e(\d+)_([.0-9]+)\.pth', os.path.basename(args.weights))
        if not m:
            raise RuntimeError('could not parse model name')

        args.config = f'config/{m.group(1)}.yml'
        args.fold = int(m.group(2))

        print(f'detected config={args.config} fold={args.fold}')

    config = load_config(args.config, args.fold)

    if args.num_epochs:
        config.train.num_epochs = args.num_epochs

    if args.num_ttas:
        config.test.num_ttas = args.num_ttas
        # config.test.batch_size //= args.num_ttas # ideally, I'd like to use big batches

    if not os.path.exists(config.experiment_dir):
        os.makedirs(config.experiment_dir)

    threshold_files = {os.path.basename(path): path for path in glob(CONFIG_PATH + '*.yml')}
    assert len(threshold_files)

    log_filename = 'log_predict.txt' if args.predict_oof or args.predict_test \
                    else 'log_training.txt'
    logger = create_logger(os.path.join(config.experiment_dir, log_filename))
    run()
