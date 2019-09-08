
import time
import logging
import random
import numpy as np
from tqdm import tqdm

import utils
from pytorch_transformers.modeling_bert import BertForPreTraining
from pytorch_transformers.tokenization_bert import BertTokenizer
from pytorch_transformers.optimization import AdamW, WarmupLinearSchedule

from torch.utils.data import DataLoader, RandomSampler
import torch
import torch_xla
import torch_xla_py.xla_model as tpu_xm
import torch_xla_py.data_parallel as tpu_dp


def main():
    parser = utils.get_args_parser_with_general_args()
    parser.add_argument("--tpu_ip", type=str, default="", help="TPU IP address")
    parser.add_argument('--tpu_report', action='store_true', help="Print xla metric report")
    parser.add_argument('--one_tpu', action='store_true', help="Run on one tpu for degugging")
    args = parser.parse_args()

    utils.init_logger(args)
    utils.set_seeds(args)
    samples_per_epoch, num_data_epochs = utils.get_samples_per_epoch(args)
    utils.make_output_dir(args)

    logging.info(f"TPU_IP: {args.tpu_ip}")
    devices = tpu_xm.get_xla_supported_devices()
    if args.one_tpu:
        devices = [devices[0]]
    n_tpu = len(devices)
    logging.info(f'Found {n_tpu} TPU cores')

    tokenizer = BertTokenizer.from_pretrained(args.bert_model, do_lower_case=args.do_lower_case)
    tokenizer.save_pretrained(args.output_dir)

    model = BertForPreTraining.from_pretrained(args.bert_model)
    model = tpu_dp.DataParallel(model, device_ids=devices)

    start_epoch = 0  # TODO: continue
    total_train_examples = 0
    for i in range(start_epoch, args.epochs):
        # The modulo takes into account the fact that we may loop over limited epochs of data
        total_train_examples += samples_per_epoch[i % len(samples_per_epoch)]

    num_train_optimization_steps = int(
        total_train_examples / args.train_batch_size / n_tpu)

    def tpu_training_loop(model, loader, device, context):
        param_optimizer = list(model.named_parameters())
        no_decay = ['bias', 'LayerNorm.bias', 'LayerNorm.weight']
        optimizer_grouped_parameters = [
            {'params': [p for n, p in param_optimizer if not any(nd in n for nd in no_decay)], 'weight_decay': 0.01},
            {'params': [p for n, p in param_optimizer if any(nd in n for nd in no_decay)], 'weight_decay': 0.0}
        ]
        optimizer = context.getattr_or(
            'optimizer',
            AdamW(optimizer_grouped_parameters, lr=args.learning_rate, eps=args.adam_epsilon))
        scheduler = context.getattr_or(
            'scheduler',
            WarmupLinearSchedule(optimizer, warmup_steps=args.warmup_steps, t_total=num_train_optimization_steps))

        tracker = tpu_xm.RateTracker()
        model.train()

        tr_loss = None
        pbar = None
        if str(pbar_device) == str(device):
            pbar = tqdm(total=int(pbar_steps), desc=f"device {device}", dynamic_ncols=True)

        for step, batch in loader:
            input_ids, input_mask, segment_ids, lm_label_ids, is_next = batch
            outputs = model(input_ids, segment_ids, input_mask, lm_label_ids, is_next)
            loss = outputs[0]
            loss.backward()
            tracker.add(args.train_batch_size)
            tpu_xm.optimizer_step(optimizer)
            scheduler.step()
            optimizer.zero_grad()

            if pbar is not None:
                pbar.update(1)

            tr_loss = loss if step == 0 else  tr_loss + loss

        if pbar is not None:
            pbar.close()
        return tr_loss.item()/step

    logging.info(f'Start training from epoch {start_epoch}')
    for epoch in range(start_epoch, args.epochs):
        epoch_dataset = utils.PregeneratedDataset(epoch=epoch, training_path=args.pregenerated_data, tokenizer=tokenizer,
                                            num_data_epochs=num_data_epochs, reduce_memory=args.reduce_memory)
        train_sampler = RandomSampler(epoch_dataset)
        train_dataloader = DataLoader(epoch_dataset, sampler=train_sampler, batch_size=args.train_batch_size)

        pbar_device = devices[0]
        pbar_steps = int(train_sampler.num_samples / args.train_batch_size / n_tpu)

        logging.info(f'start training, epoch {epoch} on {len(devices)} cores for {pbar_steps} steps')
        start = time.time()
        losses = model(tpu_training_loop, train_dataloader)
        log_message = f'Epoch {epoch} took {round(time.time() - start, 2)} seconds. Average loss: {sum(losses)/len(losses)}'
        logging.info(log_message)
        logging.info(f"Saving fine-tuned model on {args.output_dir}")
        model._models[0].save_pretrained(args.output_dir)  # TODO: continue

    if args.tpu_report:
        logging.info(torch_xla._XLAC._xla_metrics_report())

if __name__ == '__main__':
    main()
