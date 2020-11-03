# Copyright (c) 2017-present, Facebook, Inc.
# All rights reserved.
#
# This source code is licensed under the license found in the LICENSE file in
# the root directory of this source tree. An additional grant of patent rights
# can be found in the PATENTS file in the same directory.
#

import logging
import os
import torch
import traceback
import io
import numpy as np

from torch.autograd import Variable
from torch.serialization import default_restore_location

from fairseq import criterions, models


def parse_args_and_arch(parser):
    args = parser.parse_args()
    args.model = models.arch_model_map[args.arch]
    args = getattr(models, args.model).parse_arch(args)
    return args


def build_model(args, src_dict, dst_dict):
    assert hasattr(models, args.model), 'Missing model type'
    return getattr(models, args.model).build_model(args, src_dict, dst_dict)


def build_criterion(args, src_dict, dst_dict):
    padding_idx = dst_dict.pad()
    # sequence-level training
    if args.seq_criterion in criterions.sequence_criterions:
        sequence_criterion = criterions.__dict__[args.seq_criterion](args, dst_dict)

        # combine token-level and sequence-level training
        if args.seq_combined_loss_alpha > 0:
            sequence_criterion = criterions.CombinedSequenceCriterion(
                args, dst_dict,
                build_token_criterion(args, padding_idx),
                sequence_criterion,
                args.seq_combined_loss_alpha)

        return sequence_criterion
    else:
        return build_token_criterion(args, padding_idx)


def build_token_criterion(args, padding_idx):
    # token-level training
    if args.label_smoothing > 0:
        return criterions.LabelSmoothedCrossEntropyCriterion(args.label_smoothing, padding_idx)
    else:
        return criterions.CrossEntropyCriterion(padding_idx)


def torch_persistent_save(*args, **kwargs):
    for i in range(3):
        try:
            return torch.save(*args, **kwargs)
        except Exception:
            if i == 2:
                logging.error(traceback.format_exc())


def save_state(filename, args, model, criterion, optimizer, lr_scheduler, optim_history=None, extra_state=None):
    if optim_history is None:
        optim_history = []
    if extra_state is None:
        extra_state = {}
    state_dict = {
        'args': args,
        'model': model.state_dict(),
        'optimizer_history': optim_history + [
            {
                'criterion_name': criterion.__class__.__name__,
                'optimizer': optimizer.state_dict(),
                'best_loss': lr_scheduler.best,
            }
        ],
        'extra_state': extra_state,
    }
    torch_persistent_save(state_dict, filename)


def load_state(filename, model, criterion, optimizer, lr_scheduler, cuda_device=None):
    if not os.path.exists(filename):
        return None, []
    if cuda_device is None:
        state = torch.load(filename)
    else:
        state = torch.load(
            filename,
            map_location=lambda s, l: default_restore_location(s, 'cuda:{}'.format(cuda_device))
        )
    state = _upgrade_state_dict(state)

    # load model parameters
    model.load_state_dict(state['model'])

    # only load optimizer and lr_scheduler if they match with the checkpoint
    optim_history = state['optimizer_history']
    last_optim = optim_history[-1]
    if last_optim['criterion_name'] == criterion.__class__.__name__:
        optimizer.load_state_dict(last_optim['optimizer'])
        lr_scheduler.best = last_optim['best_loss']

    return state['extra_state'], optim_history


def _upgrade_state_dict(state):
    """Helper for upgrading old model checkpoints."""
    # add optimizer_history
    if 'optimizer_history' not in state:
        state['optimizer_history'] = [
            {
                'criterion_name': criterions.CrossEntropyCriterion.__name__,
                'optimizer': state['optimizer'],
                'best_loss': state['best_loss'],
            },
        ]
        del state['optimizer']
        del state['best_loss']
    # move extra_state into sub-dictionary
    if 'epoch' in state and 'extra_state' not in state:
        state['extra_state'] = {
            'epoch': state['epoch'],
            'batch_offset': state['batch_offset'],
            'val_loss': state['val_loss'],
        }
        del state['epoch']
        del state['batch_offset']
        del state['val_loss']
    return state


def load_ensemble_for_inference(filenames, src_dict, dst_dict):
    # load model architectures and weights
    states = []
    for filename in filenames:
        if not os.path.exists(filename):
            raise IOError('Model file not found: {}'.format(filename))
        states.append(
            torch.load(filename, map_location=lambda s, l: default_restore_location(s, 'cpu'))
        )
    args = states[0]['args']

    # build ensemble
    ensemble = []
    for state in states:
        model = build_model(args, src_dict, dst_dict)
        model.load_state_dict(state['model'])
        ensemble.append(model)
    return ensemble


def prepare_sample(sample, volatile=False, cuda_device=None):
    """Wrap input tensors in Variable class."""

    def make_variable(tensor):
        if cuda_device is not None and torch.cuda.is_available():
            tensor = tensor.cuda(async=True, device=cuda_device)
        return Variable(tensor, volatile=volatile)

    return {
        'id': sample['id'],
        'ntokens': sample['ntokens'],
        'target': make_variable(sample['target']),
        'net_input': {
            key: make_variable(sample[key])
            for key in ['src_tokens', 'src_positions', 'input_tokens', 'input_positions']
        },
    }


def lstrip_pad(tensor, pad):
    return tensor[tensor.eq(pad).sum():]


def rstrip_pad(tensor, pad):
    strip = tensor.eq(pad).sum()
    if strip > 0:
        return tensor[:-strip]
    return tensor

#-----------new changes below--------------------
def print_embed_overlap(embed_dict, vocab_dict):
     embed_keys = set(embed_dict.keys())
     vocab_keys = set(vocab_dict.symbols)
     overlap = len(embed_keys & vocab_keys)
     print("| Found {}/{} types in embedding file.".format(overlap, len(vocab_dict)))

def parse_embedding(embed_path):
    """Parse embedding text file into a dictionary of word and embedding tensors.

    The first line can have vocabulary size and dimension. The following lines
    should contain word and embedding separated by spaces.

    Example:
        2 5
        the -0.0230 -0.0264  0.0287  0.0171  0.1403
        at -0.0395 -0.1286  0.0275  0.0254 -0.0932
    """
#     embed_dict = dict()
#     with open(embed_path) as f_embed:
#         _ = next(f_embed) #skip header
#         for line in f_embed:
#             pieces = line.strip().split()
#             embed_dict[pieces[0]] = torch.Tensor([float(weight) for weight in pieces[1:]])

        # loading GloVe
    embedd_dict = {}
    word = None
    with io.open(embed_path, 'r', encoding='utf-8') as f:
        # skip first line
        for i, line in enumerate(f):
            if i == 0:
                continue
            word, vec = line.split(' ', 1)
            embedd_dict[word] = np.fromstring(vec, sep=' ')
    embedd_dim = len(embedd_dict[word])
    for k, v in embedd_dict.items():
        if len(v) != embedd_dim:
            print(len(v),embedd_dim)
    return embedd_dict

def load_embedding(embed_dict, vocab, embedding):
    for idx in range(len(vocab)):
        token = vocab[idx]
        if token in embed_dict:
            embedding.weight.data[idx] = torch.tensor(embed_dict[token])
    return embedding
