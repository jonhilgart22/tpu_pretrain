
import time
import logging
import random
import numpy as np
from tqdm import tqdm

import utils  # Most of the code in adopted from https://github.com/huggingface/pytorch-transformers/blob/master/examples/lm_finetuning/finetune_on_pregenerated.py
from transformers import AutoModelWithLMHead
from pytorch_transformers.tokenization_auto import AutoTokenizer
from pytorch_transformers.optimization import AdamW, WarmupLinearSchedule

from torch.utils.data import DataLoader, RandomSampler
import torch
import torch_xla
import torch_xla.core.xla_model as tpu_xm
#import torch_xla_py.xla_model as tpu_xm
import torch_xla.distributed.data_parallel as tpu_dp
import torch_xla.core.xla_model as xm
#import torch_xla_py.data_parallel as tpu_dp

from pytorch_transformers.modeling_roberta import RobertaModel
def RobertaModel_forward(self, input_ids, token_type_ids=None, attention_mask=None, position_ids=None, head_mask=None):
    return super(RobertaModel, self).forward(input_ids, token_type_ids, attention_mask, position_ids, head_mask)
RobertaModel.forward = RobertaModel_forward  # RobertaModel.forward has a `.item()` which doesn't work nicely with TPUs

def main():
    parser = utils.get_args_parser_with_general_args()
    parser.add_argument('--one_tpu', action='store_true', help="Run on one tpu core for degugging. Makes it easy to use break points")
    parser.add_argument('--tpu_report', action='store_true', help="Print xla metric report")
    args = parser.parse_args()

    utils.init(args)  # set seeds, init logger, prepare output directory

    devices = tpu_xm.get_xla_supported_devices()
    if args.one_tpu:
        devices = [devices[0]]
    n_tpu = len(devices)
    logging.info(f'Found {n_tpu} TPU cores')

    tokenizer = AutoTokenizer.from_pretrained(args.bert_model)
    tokenizer.save_pretrained(args.output_dir)

    args.start_epoch = utils.prepare_last_checkpoint(args.bert_model)
    model = AutoModelWithLMHead.from_pretrained(args.bert_model)  # Only Masked Language Modeling
    logging.info(f"Saving initial checkpoint to: {args.output_dir}")
    #model.save_pretrained(args.output_dir) #TODO: why does this break?
    #xm.save(model.state_dict(), args.output_dir)
    model = tpu_dp.DataParallel(model, device_ids=devices)

    num_data_epochs, num_train_optimization_steps= utils.get_dataset_stats(args, n_tpu)

    def tpu_training_loop(model, loader, device, context):
        """ Called by torch_xla_py.data_parallel. This function is executed on each core of the TPU once per epoch"""

        param_optimizer = list(model.named_parameters())
        no_decay = ['bias', 'LayerNorm.bias', 'LayerNorm.weight']
        optimizer_grouped_parameters = [
            {'params': [p for n, p in param_optimizer if not any(nd in n for nd in no_decay)], 'weight_decay': 0.01},
            {'params': [p for n, p in param_optimizer if any(nd in n for nd in no_decay)], 'weight_decay': 0.0}
        ]

        # one optimizer and scheduler per TPU core. Both objects are saved in `context` to be reused the next epoch
        optimizer = context.getattr_or(
            'optimizer',
            AdamW(optimizer_grouped_parameters, lr=args.learning_rate, eps=args.adam_epsilon, betas=tuple(args.betas)))

        # derive warmup info
        if args.warmup_proportion is not None:
            warmup_steps = int(args.warmup_proportion * num_train_optimization_steps + 0.5)
        elif args.warmup_steps is not None:
            warmup_steps = args.warmup_steps
        else:
            raise Exception('What is the warmup?? Specify either warmup proportion or steps')
        scheduler = context.getattr_or(
            'scheduler',
            WarmupLinearSchedule(optimizer, warmup_steps=warmup_steps, t_total=num_train_optimization_steps))

        tr_loss = None
        pbar = None
        if str(pbar_device) == str(device):  # All threads are in sync. Use progress bar only on one of them
            pbar = tqdm(total=int(pbar_steps), desc=f"device {device}", dynamic_ncols=True)

        tracker = tpu_xm.RateTracker()

        model.train()

        for step, batch in enumerate(loader):
            input_ids, input_mask, segment_ids, lm_label_ids, _ = batch
            outputs = model(input_ids, segment_ids, input_mask, lm_label_ids)
            loss = outputs[0]
            if args.gradient_accumulation_steps > 1:
                loss = loss / args.gradient_accumulation_steps
            print(loss.shape, 'loss')
            loss.sum().backward() # for multiple tensors
            tracker.add(args.train_batch_size)

            tr_loss = loss * args.gradient_accumulation_steps if step == 0 else  tr_loss + loss * args.gradient_accumulation_steps
            if pbar is not None:
                pbar.update(1)
                # pbar.set_description(desc=f'LR: {scheduler.get_lr()}')
            if (step + 1) % args.gradient_accumulation_steps == 0:
                tpu_xm.optimizer_step(optimizer)
                prev_lr = scheduler.get_last_lr()[0]
                scheduler.step()
                curr_lr = scheduler.get_last_lr()[0]
                if args.track_learning_rate:
                    if pbar is not None:
                        pbar.set_description(f"Prev LR: {prev_lr} Curr LR: {curr_lr}")
                optimizer.zero_grad()

        return tr_loss.item() / step  # `.item()` requires a trip from TPU to CPU, which is very slow. Use it only once per epoch=

    for epoch in range(args.start_epoch, args.epochs):
        # Load one training file into memory
        epoch_dataset = utils.PregeneratedDataset(epoch=epoch, training_path=args.pregenerated_data, tokenizer=tokenizer,
                                            num_data_epochs=num_data_epochs, reduce_memory=args.reduce_memory)
        train_sampler = RandomSampler(epoch_dataset)
        train_dataloader = DataLoader(epoch_dataset, sampler=train_sampler, batch_size=args.train_batch_size)

        pbar_device = devices[0]
        pbar_steps = utils.compute_num_steps_in_epoch(num_samples=train_sampler.num_samples,
                                                      batch_size=args.train_batch_size,
                                                      grad_accum_steps=1, # the pbar steps should not take into account grad accumulation steps
                                                      n_tpu=n_tpu)
        logging.info(f'start training, epoch {epoch} on {len(devices)} cores for {pbar_steps} steps')
        start = time.time()
        losses = model(tpu_training_loop, train_dataloader)  # calls `tpu_training_loop` multiple times, once per TPU core
        logging.info(f'Epoch {epoch} took {round(time.time() - start, 2)} seconds. Average loss: {sum(losses)/len(losses)}')
        utils.save_checkpoint(model._models[0], epoch, args.output_dir)

    if args.tpu_report:
        logging.info(torch_xla._XLAC._xla_metrics_report())

if __name__ == '__main__':
    main()
