import math
import torch

from typing import Any, Tuple
from torch.optim import Optimizer

class CosineLRWithRestarts():
    ''' Decays learning rate with cosine annealing, normalizes weight decay
    hyperparameter value, implements restarts.
    https://arxiv.org/abs/1711.05101

    Args:
        optimizer (Optimizer): Wrapped optimizer.
        batch_size: minibatch size
        epoch_size: training samples per epoch
        restart_period: epoch count in the first restart period
        period_inc: period increment value
        period_max: maximum period value, in epochs


    Example:
        >>> scheduler = CosineLRWithRestarts(optimizer, 32, 1024, restart_period=5, period_inc=1)
        >>> for epoch in range(100):
        >>>     scheduler.epoch_step()
        >>>     train(...)
        >>>         ...
        >>>         optimizer.zero_grad()
        >>>         loss.backward()
        >>>         optimizer.step()
        >>>         scheduler.step()
        >>>     validate(...)
    '''

    def __init__(self, optimizer, batch_size, epoch_size, restart_period=100,
                 period_inc=2, max_period=100, last_epoch=-1, eta_threshold=1000,
                 verbose=False, min_lr=1e-7):
        if not isinstance(optimizer, Optimizer):
            raise TypeError('{} is not an Optimizer'.format(
                type(optimizer).__name__))

        self.optimizer = optimizer
        if last_epoch == -1:
            for group in optimizer.param_groups:
                group.setdefault('initial_lr', group['lr'])
        else:
            for i, group in enumerate(optimizer.param_groups):
                if 'initial_lr' not in group:
                    raise KeyError("param 'initial_lr' is not specified "
                                   "in param_groups[{}] when resuming an"
                                   " optimizer".format(i))

        self.base_lrs = list(map(lambda group: group['initial_lr'],
                                 optimizer.param_groups))

        self.last_epoch = last_epoch
        self.batch_size = batch_size
        self.epoch_size = epoch_size
        self.eta_threshold = eta_threshold
        self.period_inc = period_inc
        self.max_period = max_period
        self.verbose = verbose
        self.base_weight_decays = list(map(lambda group: group['weight_decay'],
                                           optimizer.param_groups))
        self.restart_period = restart_period
        self.restarts = 0
        self.t_epoch = -1
        self.min_lr = min_lr

    def _schedule_eta(self) -> Tuple[float, float]:
        ''' Threshold value could be adjusted to shrink eta_min and eta_max values. '''
        eta_min = 0
        eta_max = 1
        if self.restarts <= self.eta_threshold:
            return eta_min, eta_max
        else:
            d = self.restarts - self.eta_threshold
            k = d * 0.09
            return (eta_min + k, eta_max - k)

    def get_lr(self, t_cur: int) -> Any:
        eta_min, eta_max = self._schedule_eta()

        eta_t = (eta_min + 0.5 * (eta_max - eta_min)
                 * (1. + math.cos(math.pi *
                                  (t_cur / self.restart_period))))

        weight_decay_norm_multi = math.sqrt(self.batch_size /
                                            (self.epoch_size *
                                             self.restart_period))
        lrs = [base_lr * eta_t for base_lr in self.base_lrs]
        weight_decays = [base_weight_decay * eta_t * weight_decay_norm_multi
                         for base_weight_decay in self.base_weight_decays]

        return zip(lrs, weight_decays)

    def _set_batch_size(self) -> None:
        d, r = divmod(self.epoch_size, self.batch_size)
        batches_in_epoch = d + 2 if r > 0 else d + 1
        self.batch_increment = iter(torch.linspace(0, 1, batches_in_epoch))

    def epoch_step(self) -> bool:
        ''' Returns true if we started new cosine anneal period this epoch. '''
        self.last_epoch += 1
        self.t_epoch += 1
        self._set_batch_size()
        return self.step()

    def step(self) -> bool:
        res = False
        t_cur = self.t_epoch + next(self.batch_increment)

        for param_group, (lr, weight_decay) in zip(self.optimizer.param_groups,
                                                   self.get_lr(t_cur)):
            param_group['lr'] = max(lr, self.min_lr)
            param_group['weight_decay'] = weight_decay

        if self.t_epoch % self.restart_period < self.t_epoch:
            res = True
            if self.verbose:
                print("restart at epoch {}".format(self.last_epoch))

            self.restart_period = min(self.restart_period + self.period_inc,
                                      self.max_period)
            self.restarts += 1
            self.t_epoch = 0

        return res
