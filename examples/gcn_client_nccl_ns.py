import os
import sys
# set environment
module_name ='PaGraph'
modpath = os.path.abspath('.')
if module_name in modpath:
  idx = modpath.find(module_name)
  modpath = modpath[:idx]
sys.path.append(modpath)

import argparse, time
import torch
import torch.nn as nn
import torch.distributed as dist
import torch.multiprocessing as mp
import torch.nn.functional as F
import numpy as np
import dgl

from PaGraph.model.gcn_ns import GCNSampling, GCNInfer
import PaGraph.data as data

def init_process(rank, world_size, backend):
  os.environ['MASTER_ADDR'] = '127.0.0.1'
  os.environ['MASTER_PORT'] = '29501'
  dist.init_process_group(backend, rank=rank, world_size=world_size)
  torch.cuda.set_device(rank)
  torch.manual_seed(rank)
  print('rank [{}] process successfully launches'.format(rank))

def trainer(rank, world_size, args, backend='nccl'):
  # init multi process
  init_process(rank, world_size, backend)
  
  # load data
  dataname = os.path.basename(args.dataset)
  g = dgl.contrib.graph_store.create_graph_from_store(dataname, "shared_mem")
  labels = data.get_labels(args.dataset)
  n_classes = len(np.unique(labels))
  # masks for semi-supervised learning
  train_mask, val_mask, test_mask = data.get_masks(args.dataset)
  train_nid = np.nonzero(train_mask)[0].astype(np.int64)
  test_nid = np.nonzero(test_mask)[0].astype(np.int64)
  # to torch tensor
  labels = torch.LongTensor(labels)
  train_mask = torch.ByteTensor(train_mask)
  val_mask = torch.ByteTensor(val_mask)
  test_mask = torch.ByteTensor(test_mask)

  # prepare model
  model = GCNSampling(args.feat_size,
                      args.n_hidden,
                      n_classes,
                      args.n_layers,
                      F.relu,
                      args.dropout)
  loss_fcn = torch.nn.CrossEntropyLoss()
  optimizer = torch.optim.Adam(model.parameters(),
                               lr=args.lr,
                               weight_decay=args.weight_decay)
  model.cuda(rank)
  model = torch.nn.parallel.DistributedDataParallel(model, device_ids=[rank])
  ctx = torch.device(rank)

  # start training
  epoch_dur = []
  batch_dur = []
  for epoch in range(args.n_epochs):
    model.train()
    epoch_start_time = time.time()
    step = 0
    for nf in dgl.contrib.sampling.NeighborSampler(g, args.batch_size,
                                                  args.num_neighbors,
                                                  neighbor_type='in',
                                                  shuffle=True,
                                                  num_workers=8,
                                                  num_hops=args.n_layers+1,
                                                  seed_nodes=train_nid,
                                                  prefetch=True):
      batch_start_time = time.time()
      nf.copy_from_parent(ctx=ctx)
      batch_nids = nf.layer_parent_nid(-1)
      label = labels[batch_nids]
      label = label.cuda(rank, non_blocking=True)
      
      pred = model(nf)
      loss = loss_fcn(pred, label)
      optimizer.zero_grad()
      loss.backward()
      optimizer.step()

      step += 1
      batch_dur.append(time.time() - batch_start_time)
      if rank == 0 and step % 1 == 0:
        print('epoch [{}] step [{}]. Loss: {:.4f} Batch average time(s): {:.4f}'
              .format(epoch + 1, step, loss.item(), np.mean(np.array(batch_dur))))
    if rank == 0:
      epoch_dur.append(time.time() - epoch_start_time)
      print('Epoch average time: {:.4f}'.format(np.mean(np.array(epoch_dur[2:]))))
  
  if rank == 0:
    infer_model = GCNInfer(in_feats,
                   args.n_hidden,
                   n_classes,
                   args.n_layers,
                   F.relu)
    infer_model.cuda(ctx)
    for infer_param, param in zip(infer_model.parameters(), model.module.parameters()):    
      infer_param.data.copy_(param.data)
    num_acc = 0.
    for nf in dgl.contrib.sampling.NeighborSampler(g, args.batch_size,
                                                         g.number_of_nodes(),
                                                         neighbor_type='in',
                                                         num_workers=32,
                                                         num_hops=args.n_layers+1,
                                                         seed_nodes=test_nid):
      nf.copy_from_parent(ctx=ctx)
      infer_model.eval()
      with torch.no_grad():
        pred = infer_model(nf)
        batch_nids = nf.layer_parent_nid(-1).to(device=pred.device, dtype=torch.long)
        batch_labels = labels[batch_nids].to(ctx)
        num_acc += (pred.argmax(dim=1) == batch_labels).sum().cpu().item()

    print("Test Accuracy {:.4f}".format(num_acc / n_test_samples))


if __name__ == '__main__':
  parser = argparse.ArgumentParser(description='GCN')

  parser.add_argument("--gpu", type=str, default='cpu',
                      help="gpu ids. such as 0 or 0,1,2")
  parser.add_argument("--dataset", type=str, default=None,
                      help="path to the dataset folder")
  # model arch
  parser.add_argument("--feat-size", type=int, default=300,
                      help='input feature size')
  parser.add_argument("--dropout", type=float, default=0.5,
                      help="dropout probability")
  parser.add_argument("--n-hidden", type=int, default=32,
                      help="number of hidden gcn units")
  parser.add_argument("--n-layers", type=int, default=1,
                      help="number of hidden gcn layers")
  # training hyper-params
  parser.add_argument("--lr", type=float, default=3e-2,
                      help="learning rate")
  parser.add_argument("--n-epochs", type=int, default=60,
                      help="number of training epochs")
  parser.add_argument("--batch-size", type=int, default=10000,
                      help="batch size")
  parser.add_argument("--weight-decay", type=float, default=5e-4,
                      help="Weight for L2 loss")
  # sampling hyper-params
  parser.add_argument("--num-neighbors", type=int, default=10,
                      help="number of neighbors to be sampled")
  
  args = parser.parse_args()

  os.environ['CUDA_VISIBLE_DEVICES'] = args.gpu
  gpu_num = len(args.gpu.split(','))

  mp.spawn(trainer, args=(gpu_num, args), nprocs=gpu_num, join=True)
  