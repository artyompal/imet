import logging, os
import inspect, re
from typing import Any, Optional

import numpy as np
import torch


def create_logger(filename: Optional[str], onscreen: bool = True) -> Any:
    logger_name = os.path.basename(filename) if filename is not None else 'log'
    file_fmt_str = '%(asctime)s %(message)s'
    console_fmt_str = '%(message)s'
    file_level = logging.DEBUG
    console_level = logging.DEBUG

    logger = logging.getLogger(logger_name)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    if filename is not None:
        file_fmt = logging.Formatter(file_fmt_str, '%m-%d %H:%M:%S')
        log_file = logging.FileHandler(filename)
        log_file.setLevel(file_level)
        log_file.setFormatter(file_fmt)
        logger.addHandler(log_file)

    if onscreen:
        console_fmt = logging.Formatter(console_fmt_str)
        log_console = logging.StreamHandler()
        log_console.setLevel(logging.DEBUG)
        log_console.setFormatter(console_fmt)
        logger.addHandler(log_console)

    return logger

class AverageMeter(object):
    """Computes and stores the average and current value"""
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.val = 0.0
        self.avg = 0.0
        self.sum = 0.0
        self.count = 0

    def update(self, val: float, n: int = 1) -> None:
        self.val = val
        self.sum += val * n
        self.count += n
        self.avg = self.sum / self.count


def F_score(predict: torch.tensor, label: torch.tensor, threshold: float = 0.5,
            beta: int = 2) -> float:
    predict = predict > threshold
    label = label > threshold

    TP = (predict & label).sum(1).float()
    TN = ((~predict) & (~label)).sum(1).float()
    FP = (predict & (~label)).sum(1).float()
    FN = ((~predict) & label).sum(1).float()

    precision = TP / (TP + FP + 1e-12)
    recall = TP / (TP + FN + 1e-12)
    F2 = (1 + beta**2) * precision * recall / (beta**2 * precision + recall + 1e-12)
    return F2.mean(0)
