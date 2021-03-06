#!/usr/bin/python3.6

import itertools, os, re, sys
from glob import glob
from typing import List
from debug import dprint

IN_KERNEL = os.environ.get('KAGGLE_WORKING_DIR') is not None
MODEL_PATH = '../input/' if IN_KERNEL else '../best_models/'

def run(command: List[str]) -> None:
    res = os.system('export PYTHONPATH=${PYTHONPATH}:/kaggle/working && ' + ' '.join(command))
    if res != 0:
        sys.exit()

num_tta = 4

models = {
    '4b_se_resnext101_352x352_f0_e16_0.6050.pth': 1,
    '4b_se_resnext101_352x352_f1_e29_0.6079.pth': 1,
    '4b_se_resnext101_352x352_f2_e24_0.6022.pth': 1,
    '4b_se_resnext101_352x352_f3_e21_0.6047.pth': 1,
    '4b_se_resnext101_352x352_f4_e24_0.6050.pth': 1,
    }

model2path = {os.path.basename(path): path for path in glob(MODEL_PATH + '**/*.pth')}
for model in models.keys():
    assert os.path.exists(model2path[model])

for model in models.keys():
    m = re.match(r'(.*)_f(\d)_e\d+.*\.pth', os.path.basename(model))
    assert m

    script_name = f'train_{m.group(1)}.py'
    fold = m.group(2)

    cmd = ['python3.6', script_name, '--predict', '--weights', model2path[model],
           '--fold', fold, '--num_tta', str(num_tta)]
    print('running', cmd)
    run(cmd)

cmd = ['python3.6', 'blend.py', 'submission.csv']

for model, weight in models.items():
    name = os.path.splitext(os.path.basename(model))[0]
    predict = f'pred_level1_{name}.npz'
    cmd.extend([predict, str(weight)])

print('running', cmd)
run(cmd)
