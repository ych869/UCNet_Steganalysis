#!/usr/bin/env python3

import os
import argparse
import numpy as np
import cv2
from pathlib import Path
import copy
import logging
import random
import scipy.io as sio
import time
import math

import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data.dataset import Dataset
from torch.utils.data import DataLoader
from torchvision import transforms
from torch.nn.parameter import Parameter
import torch.nn.functional as F
from srm_filter_kernel import all_normalized_hpf_list


IMAGE_SIZE = 256
BATCH_SIZE = 16
LR = 0.01
WEIGHT_DECAY = 5e-4

TRAIN_FILE_COUNT = 14000
TRAIN_PRINT_FREQUENCY = 100
EVAL_PRINT_FREQUENCY = 1

OUTPUT_PATH = Path(__file__).stem


class TLU(nn.Module):
    def __init__(self, threshold):
        super(TLU, self).__init__()

        self.threshold = threshold

    def forward(self, input):
        output = torch.clamp(input, min=-self.threshold, max=self.threshold)

        return output


def build_filters():
    filters = []
    ksize = [5]
    lamda = np.pi / 2.0
    sigma = [0.5, 1.0]
    phi = [0, np.pi / 2]
    for hpf_item in all_normalized_hpf_list:
        row_1 = int((5 - hpf_item.shape[0]) / 2)
        row_2 = int((5 - hpf_item.shape[0]) - row_1)
        col_1 = int((5 - hpf_item.shape[1]) / 2)
        col_2 = int((5 - hpf_item.shape[1]) - col_1)
        hpf_item = np.pad(hpf_item, pad_width=((row_1, row_2), (col_1, col_2)), mode='constant')
        filters.append(hpf_item)
    for theta in np.arange(0, np.pi, np.pi / 8):  # gabor 0 22.5 45 67.5 90 112.5 135 157.5
        for k in range(2):
            for j in range(2):
                kern = cv2.getGaborKernel((ksize[0], ksize[0]), sigma[k], theta, sigma[k] / 0.56, 0.5, phi[j],
                                          ktype=cv2.CV_32F)
                filters.append(kern)
    return filters


class HPF(nn.Module):
    def __init__(self):
        super(HPF, self).__init__()

        filt_list = build_filters()
        hpf_weight = nn.Parameter(torch.Tensor(filt_list).view(62, 1, 5, 5), requires_grad=False)

        self.hpf = nn.Conv2d(1, 62, kernel_size=5, padding=2, bias=False)
        self.hpf.weight = hpf_weight

        self.tlu = TLU(2.0)  # T= 2

    def forward(self, input):
        output = self.hpf(input)
        output = self.tlu(output)

        return output  

      
class Type1(nn.Module):
    def __init__(self, inchannel, outchannel):
        super(Type1, self).__init__()
        self.inchannel=inchannel
        self.outchannel=outchannel
        self.relu = nn.ReLU()

        self.basic=nn.Sequential(
                nn.Conv2d(inchannel, outchannel, kernel_size=3, padding=1),
                nn.BatchNorm2d(outchannel),
                nn.ReLU(),
                
                nn.Conv2d(outchannel, outchannel, kernel_size=3, padding=1),
                nn.BatchNorm2d(outchannel),
                nn.ReLU()
                )

    def forward(self,x):
        out=self.basic(x)

        return out
      
    
class Type2(nn.Module):
    def __init__(self, inchannel, outchannel):
        super(Type2, self).__init__()
        self.inchannel=inchannel
        self.outchannel=outchannel
        self.relu = nn.ReLU()

        self.basic=nn.Sequential(
                nn.Conv2d(inchannel, inchannel, kernel_size=3, padding=1),
                nn.BatchNorm2d(inchannel),
                nn.ReLU(),
                
                nn.Conv2d(inchannel, outchannel, kernel_size=3, padding=1),
                nn.BatchNorm2d(outchannel),
            
                nn.AvgPool2d(kernel_size=3, padding=1, stride=2),
                )
        self.shortcut=nn.Sequential(
                nn.Conv2d(inchannel, outchannel, kernel_size=1, stride=2),
                nn.BatchNorm2d(outchannel),
                )
    def forward(self,x):
        out=self.basic(x)
        out+=self.shortcut(x)
        out=self.relu(out)

        return out
        
        
class Type3(nn.Module):
    def __init__(self, inchannel, outchannel):
        super(Type3, self).__init__()
        self.inchannel=inchannel
        self.outchannel=outchannel
        self.relu=nn.ReLU()

        self.basic=nn.Sequential(
                nn.Conv2d(inchannel, outchannel, kernel_size=1),
                nn.BatchNorm2d(outchannel),
                nn.ReLU(),
                nn.Conv2d(outchannel, outchannel, kernel_size=3, stride=2, groups=32, padding=1),
                nn.BatchNorm2d(outchannel),
                nn.ReLU(),
                nn.Conv2d(outchannel, outchannel, kernel_size=1),
                nn.BatchNorm2d(outchannel),
                )
        self.shortcut=nn.Sequential(
                nn.Conv2d(inchannel, outchannel, kernel_size=3, stride=2, padding=1),
                nn.BatchNorm2d(outchannel),
                )
    def forward(self,x):
        out=self.basic(x)
        out+=self.shortcut(x)
        out=self.relu(out)
        return out
    
        
class Net(nn.Module):
  def __init__(self):
    super(Net, self).__init__()
    
    self.pre = HPF()

    self.group1 = type1(186,32)
    self.group2 = type2(32,32)
    self.group3 = type3(32,64)
    self.group4 = type2(64,128)
    self.group5 = type1(128,256)
    
    self.avg = nn.AvgPool2d(kernel_size=32, stride=1)
    self.fc1 = nn.Linear(1 * 1 * 256, 2)

  def forward(self, input):
    output = input
    
    # seperate color channels
    output_c1 = output[:, 0, :, :]
    output_c2 = output[:, 1, :, :] 
    output_c3 = output[:, 2, :, :] 
    out_c1 = output_c1.unsqueeze(1)
    out_c2 = output_c2.unsqueeze(1)
    out_c3 = output_c3.unsqueeze(1)
    c1 = self.pre(out_c1)
    c2 = self.pre(out_c2)
    c3 = self.pre(out_c3)
    output = torch.cat([c1, c2, c3], dim=1)
    
    output = self.group1(output)
    output = self.group2(output)
    output = self.group3(output)
    output = self.group4(output)
    output = self.group5(output)
    
    output = self.avg(output)
    output = output.view(output.size(0), -1)
    output = self.fc1(output)

    return output


class AverageMeter(object):
    def __init__(self):
        self.reset()

    def reset(self):
        self.val = 0
        self.avg = 0
        self.sum = 0
        self.count = 0

    def update(self, val, n=1):
        self.val = val
        self.sum += val * n
        self.count += n
        self.avg = self.sum / self.count


def train(model, device, train_loader, optimizer, epoch):
    batch_time = AverageMeter()  
    data_time = AverageMeter()
    losses = AverageMeter()

    model.train()

    end = time.time()

    for i, sample in enumerate(train_loader):

        data_time.update(time.time() - end)

        data, label = sample['data'], sample['label']

        shape = list(data.size())
        data = data.reshape(shape[0] * shape[1], *shape[2:])
        label = label.reshape(-1)

        data, label = data.to(device), label.to(device)

        optimizer.zero_grad()

        end = time.time()

        output = model(data)  

        criterion = nn.CrossEntropyLoss()
        loss = criterion(output, label)

        losses.update(loss.item(), data.size(0))

        loss.backward()  
        optimizer.step()

        batch_time.update(time.time() - end)  
        end = time.time()

        if i % TRAIN_PRINT_FREQUENCY == 0:
            logging.info('Epoch: [{0}][{1}/{2}]\t'
                         'Time {batch_time.val:.3f} ({batch_time.avg:.3f})\t'
                         'Data {data_time.val:.3f} ({data_time.avg:.3f})\t'
                         'Loss {loss.val:.4f} ({loss.avg:.4f})\t'.format(
                epoch, i, len(train_loader), batch_time=batch_time,
                data_time=data_time, loss=losses))


def adjust_bn_stats(model, device, train_loader):
    model.train()

    with torch.no_grad():
        for sample in train_loader:
            data, label = sample['data'], sample['label']

            shape = list(data.size())
            data = data.reshape(shape[0] * shape[1], *shape[2:])
            label = label.reshape(-1)

            data, label = data.to(device), label.to(device)

            output = model(data)


def evaluate(model, device, eval_loader, epoch, optimizer, best_acc, PARAMS_PATH, TMP):
    model.eval()

    test_loss = 0
    correct = 0

    with torch.no_grad():
        for sample in eval_loader:
            data, label = sample['data'], sample['label']

            shape = list(data.size())
            data = data.reshape(shape[0] * shape[1], *shape[2:])
            label = label.reshape(-1)

            data, label = data.to(device), label.to(device)

            output = model(data)
            pred = output.max(1, keepdim=True)[1]
            correct += pred.eq(label.view_as(pred)).sum().item()

    accuracy = correct / (len(eval_loader.dataset) * 2)

    if accuracy > best_acc and epoch > TMP:
        best_acc = accuracy
        all_state = {
            'original_state': model.state_dict(),
            'optimizer_state': optimizer.state_dict(),
            'epoch': epoch
        }
        torch.save(all_state, PARAMS_PATH)

    logging.info('-' * 8)
    logging.info('Eval accuracy: {:.4f}'.format(accuracy))
    logging.info('Best accuracy:{:.4f}'.format(best_acc))
    logging.info('-' * 8)
    return best_acc


def initWeights(module):
    if type(module) == nn.Conv2d:
        if module.weight.requires_grad:
            nn.init.kaiming_normal_(module.weight.data, mode='fan_in', nonlinearity='relu')

    if type(module) == nn.Linear:
        nn.init.normal_(module.weight.data, mean=0, std=0.01)
        nn.init.constant_(module.bias.data, val=0)

    if type(module) == nn.GroupNorm:
        nn.init.constant_(module.weight.data, 1)
        nn.init.constant_(module.bias.data, 0)


class AugData():
    def __call__(self, sample):
        data, label = sample['data'], sample['label']

        rot = random.randint(0, 3)

        data = np.rot90(data, rot, axes=[2, 3]).copy()  # gaiwei [2,3]

        if random.random() < 0.5:
            data = np.flip(data, axis=2).copy()

        new_sample = {'data': data, 'label': label}

        return new_sample


class ToTensor():
    def __call__(self, sample):
        data, label = sample['data'], sample['label']

        data = data.astype(np.float32)

        new_sample = {
            'data': torch.from_numpy(data),
            'label': torch.from_numpy(label).long(),
        }

        return new_sample


class MyDataset(Dataset):
    def __init__(self, index_path, ALASKA_COVER_DIR, ALASKA_STEGO_DIR, transform=None):
        self.index_list = np.load(index_path)
        self.transform = transform

        self.alaska_cover_path = ALASKA_COVER_DIR + '/{}.ppm'
        self.alaska_stego_path = ALASKA_STEGO_DIR + '/{}.ppm'

    def __len__(self):
        return self.index_list.shape[0]

    def __getitem__(self, idx):
        file_index = self.index_list[idx]

        cover_path = self.alaska_cover_path.format(file_index)
        stego_path = self.alaska_stego_path.format(file_index)

        cover_data = cv2.imread(cover_path, -1)
        cover_data = cv2.cvtColor(cover_data, cv2.COLOR_BGR2RGB)
        cover_data = np.transpose(cover_data, (2, 0, 1))
        stego_data = cv2.imread(stego_path, -1)
        stego_data = cv2.cvtColor(stego_data, cv2.COLOR_BGR2RGB)
        stego_data = np.transpose(stego_data, (2, 0, 1))

        data = np.stack([cover_data, stego_data])
        label = np.array([0, 1], dtype='int32')

        sample = {'data': data, 'label': label}

        if self.transform:
            sample = self.transform(sample)

        return sample


def setLogger(log_path, mode='a'):
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)

    if not logger.handlers:
        # Logging to a file
        file_handler = logging.FileHandler(log_path, mode=mode)
        file_handler.setFormatter(logging.Formatter('%(asctime)s: %(message)s', '%Y-%m-%d %H:%M:%S'))
        logger.addHandler(file_handler)

        # Logging to console
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(logging.Formatter('%(message)s'))
        logger.addHandler(stream_handler)


def main(args):
    statePath = args.statePath

    device = torch.device("cuda")

    kwargs = {'num_workers': 1, 'pin_memory': True}

    train_transform = transforms.Compose([
        AugData(),
        ToTensor()
    ])

    eval_transform = transforms.Compose([
        ToTensor()
    ])

    DATASET_INDEX = args.DATASET_INDEX
    STEGANOGRAPHY = args.STEGANOGRAPHY
    EMBEDDING_RATE = args.EMBEDDING_RATE

    ALASKA_COVER_DIR = '/path/xxx'
    ALASKA_STEGO_DIR = '/path/xxx_{}_{}/'.format(STEGANOGRAPHY, EMBEDDING_RATE)
 
    TRAIN_INDEX_PATH = 'index_list/alaska_train_index_14000.npy'
    VALID_INDEX_PATH = 'index_list/alaska_valid_index_1000.npy'
    TEST_INDEX_PATH = 'index_list/alaska_test_index_5000.npy'

    LOAD_RATE = float(EMBEDDING_RATE) + 0.1
    LOAD_RATE = round(LOAD_RATE, 1)

    PARAMS_NAME = '{}-{}-{}-params-lr={}.pt'.format(STEGANOGRAPHY, EMBEDDING_RATE, DATASET_INDEX,  LR)
    LOG_NAME = '{}-{}-{}-model_log-lr={}.log'.format(STEGANOGRAPHY, EMBEDDING_RATE, DATASET_INDEX, LR)

    PARAMS_PATH = os.path.join(OUTPUT_PATH, PARAMS_NAME)
    LOG_PATH = os.path.join(OUTPUT_PATH, LOG_NAME)

    PARAMS_INIT_PATH = os.path.join(OUTPUT_PATH, PARAMS_INIT_NAME)

    setLogger(LOG_PATH, mode='w')

    Path(OUTPUT_PATH).mkdir(parents=True, exist_ok=True)

    train_dataset = MyDataset(TRAIN_INDEX_PATH, ALASKA_COVER_DIR, ALASKA_STEGO_DIR, train_transform)
    valid_dataset = MyDataset(VALID_INDEX_PATH, ALASKA_COVER_DIR, ALASKA_STEGO_DIR, eval_transform)
    test_dataset = MyDataset(TEST_INDEX_PATH, ALASKA_COVER_DIR, ALASKA_STEGO_DIR, eval_transform)

    train_loader = DataLoader(train_dataset, batch_size=BATCH_SIZE, shuffle=True, **kwargs)
    valid_loader = DataLoader(valid_dataset, batch_size=BATCH_SIZE, shuffle=False, **kwargs)
    test_loader = DataLoader(test_dataset, batch_size=BATCH_SIZE, shuffle=False, **kwargs)

    model = Net().to(device)
    model.apply(initWeights)

    params = model.parameters()

    params_wd, params_rest = [], []
    for param_item in params:
        if param_item.requires_grad:
            (params_wd if param_item.dim() != 1 else params_rest).append(param_item)

    param_groups = [{'params': params_wd, 'weight_decay': WEIGHT_DECAY},
                    {'params': params_rest}]

    optimizer = optim.SGD(param_groups, lr=LR, momentum=0.9)

    EPOCHS = 250
    DECAY_EPOCH = [80, 140, 190]
    TMP = 190


    if statePath:
        logging.info('-' * 8)
        logging.info('Load state_dict in {}'.format(statePath))
        logging.info('-' * 8)

        all_state = torch.load(statePath)

        original_state = all_state['original_state']
        optimizer_state = all_state['optimizer_state']
        epoch = all_state['epoch']

        model.load_state_dict(original_state)
        optimizer.load_state_dict(optimizer_state)

        startEpoch = epoch + 1

    else:
        startEpoch = 1


    scheduler = optim.lr_scheduler.MultiStepLR(optimizer, milestones=DECAY_EPOCH, gamma=0.1)
    best_acc = 0.0
    for epoch in range(startEpoch, EPOCHS + 1):
        scheduler.step()

        train(model, device, train_loader, optimizer, epoch)

        if epoch % EVAL_PRINT_FREQUENCY == 0:
            adjust_bn_stats(model, device, train_loader)
            best_acc = evaluate(model, device, valid_loader, epoch, optimizer, best_acc, PARAMS_PATH, TMP)

    logging.info('\nTest set accuracy: \n')

    # load best parmater to test
    all_state = torch.load(PARAMS_PATH)
    original_state = all_state['original_state']
    optimizer_state = all_state['optimizer_state']
    model.load_state_dict(original_state)
    optimizer.load_state_dict(optimizer_state)

    adjust_bn_stats(model, device, train_loader)
    evaluate(model, device, test_loader, epoch, optimizer, best_acc, PARAMS_PATH, TMP)


def myParseArgs():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        '-i',
        '--DATASET_INDEX',
        help='Path for loading dataset',
        type=str,
        default=''
    )

    parser.add_argument(
        '-alg',
        '--STEGANOGRAPHY',
        help='embedding_algorithm',
        type=str,
        choices=['CMDC-HILL', 'CMDC-SUNIWARD', 'GINA-HILL', 'GINA-SUNIWARD'],
        required=True
    )

    parser.add_argument(
        '-rate',
        '--EMBEDDING_RATE',
        help='embedding_rate',
        type=str,
        choices=[ '0.4', '0.3', '0.2'],
        required=True
    )

    parser.add_argument(
        '-g',
        '--gpuNum',
        help='Determine which gpu to use',
        type=str,
        choices=['0', '1', '2', '3'],
        required=True
    )

    parser.add_argument(
        '-l',
        '--statePath',
        help='Path for loading model state',
        type=str,
        default=''
    )

    args = parser.parse_args()

    return args


if __name__ == '__main__':
    args = myParseArgs()

    os.environ['CUDA_VISIBLE_DEVICES'] = args.gpuNum
    main(args)
