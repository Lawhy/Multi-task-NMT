from mnmt.inputter import ArgsFeeder
from mnmt.inputter import generate_batch_iterators
from mnmt.inputter import DataContainer
import torch
import torch.nn as nn
import torch.optim as optim
import math
import time


class Trainer:

    data_container: DataContainer

    def __init__(self, args_feeder: ArgsFeeder, model):
        """
        Args:
            args_feeder (ArgsFeeder):
            model: the NMT model
        """
        self.model = model
        self.args_feeder = args_feeder
        # optimizer
        self.optimizer = getattr(optim, args_feeder.optim_choice)(model.parameters(), lr=args_feeder.learning_rate)

        # learning rate scheduler
        if args_feeder.valid_criterion == 'ACC':
            self.decay_mode = 'max'  # decay when less than maximum
        elif args_feeder.valid_criterion == 'LOSS':
            self.decay_mode = 'min'  # decay when more than minimum
        self.scheduler = optim.lr_scheduler.ReduceLROnPlateau(
            optimizer=self.optimizer,
            mode=self.decay_mode, factor=args_feeder.lr_decay_factor,  # 0.9 in paper
            patience=args_feeder.decay_patience)

        # evaluation memory bank
        class EvalMemoryBank:
            def __init__(self, best_valid_loss=float('inf'), acc_valid_loss=float('inf'),
                         best_valid_acc=float(-1), best_valid_epoch=float(-1), best_train_step=float(-1),
                         early_stopping_patience=args_feeder.early_stopping_patience,
                         best_valid_loss_aux=float('inf'), best_valid_acc_aux=float(-1)):
                self.best_valid_loss = best_valid_loss
                self.acc_valid_loss = acc_valid_loss
                self.best_valid_acc = best_valid_acc
                self.best_valid_epoch = best_valid_epoch
                self.best_train_step = best_train_step
                self.early_stopping_patience = early_stopping_patience
                self.best_valid_loss_aux = best_valid_loss_aux
                self.best_valid_acc_aux = best_valid_acc_aux

        self.eval_memory_bank = EvalMemoryBank()
        # to recover full patience when improving
        self.early_stopping_patience = args_feeder.early_stopping_patience

        # training memory bank
        class TrainMemoryBank:
            def __init__(self, exp_num=args_feeder.exp_num,
                         total_epochs=args_feeder.total_epochs,
                         n_epoch=0, n_steps=0,
                         report_interval=args_feeder.report_interval):
                self.exp_num = exp_num
                self.total_epochs = total_epochs
                self.n_epoch = n_epoch
                self.n_steps = n_steps
                self.report_interval = report_interval

        self.train_memory_bank = TrainMemoryBank()

        # single or multi task
        self.multi_task_ratio = args_feeder.multi_task_ratio
        if self.multi_task_ratio == 1:
            print("Running single-main-task experiment...")
            self.task = "Single-Main"
        elif self.multi_task_ratio == 0:
            print("Running single-auxiliary-task experiment...")
            self.task = "Single-Auxiliary"
        else:
            print("Running multi-task experiment...")
            self.task = "Multi"

        # data
        self.data_container = args_feeder.data_container
        self.train_iter, self.valid_iter, self.test_iter = generate_batch_iterators(self.data_container,
                                                                                    self.args_feeder.batch_size,
                                                                                    self.args_feeder.device,
                                                                                    src_lang=self.args_feeder.src_lang)
        for (name, field) in self.data_container.fields:
            if name == self.args_feeder.src_lang:
                self.src_field = field
            elif name == self.args_feeder.trg_lang:
                self.trg_field = field
            elif name == self.args_feeder.auxiliary_name:
                self.auxiliary_field = field

        # teacher forcing
        self.tfr = 0.8

        # loss function
        self.loss_function = self.construct_loss_function()

    def run(self, burning_epoch):
        try:
            for epoch in range(self.train_memory_bank.total_epochs):
                self.train_memory_bank.n_epoch = epoch
                # apply nothing during the burning phase, recall Bayesian Modelling
                if epoch <= burning_epoch:
                    print("Renew Evaluation Records in the Burning Phase...")
                    # abandon the best checkpoint in early stage
                    self.eval_memory_bank.best_valid_loss = float('inf')
                    self.eval_memory_bank.best_valid_acc = 0
                    self.eval_memory_bank.early_stopping_patience = self.early_stopping_patience

                if self.eval_memory_bank.early_stopping_patience == 0:
                    print("Early Stopping!")
                    break

                start_time = time.time()

                self.tfr = max(1 - (float(10 + epoch * 1.5) / 50), 0.2)
                train_loss = self.train()
                valid_loss, valid_acc, valid_acc_aux = self.evaluate(is_test=False)

                end_time = time.time()

                epoch_mins, epoch_secs = self.epoch_time(start_time, end_time)

                self.update(valid_loss, valid_acc)
                if self.task is "Multi":
                    self.update_aux(valid_acc_aux)

                print(f'Epoch: {epoch + 1:02} | Time: {epoch_mins}m {epoch_secs}s')
                print(f'\tTrain Loss: {train_loss:.3f} | Train PPL: {math.exp(train_loss):7.3f}')
                print(f'\t Val. Loss: {valid_loss:.3f} | '
                      f'Val. Acc: {valid_acc:.3f} | '
                      f'Val. PPL: {math.exp(valid_loss):7.3f}')
        except KeyboardInterrupt:
            print("Exiting loop")

    @staticmethod
    def epoch_time(start_time, end_time):
        """
        Args:
            start_time:
            end_time:
        """
        elapsed_time = end_time - start_time
        elapsed_mins = int(elapsed_time / 60)
        elapsed_secs = int(elapsed_time - (elapsed_mins * 60))
        return elapsed_mins, elapsed_secs

    def update(self, valid_loss, valid_acc):
        """
        Args:
            valid_loss: current validation loss
            valid_acc: current validation accuracy
        """
        valid_criterion = self.args_feeder.valid_criterion
        assert valid_criterion in ['LOSS', 'ACC']
        print("\n---------------------------------------")
        print("[Epoch: {}][Validatiing...]".format(self.train_memory_bank.n_epoch))

        # For Validation Loss
        if valid_loss <= self.eval_memory_bank.best_valid_loss:
            print('\t\t Better Valid Loss! (at least equal)')
            self.eval_memory_bank.best_valid_loss = valid_loss
            if valid_criterion == 'LOSS':
                torch.save(self.model.state_dict(),
                           'experiments/exp' + str(self.train_memory_bank.exp_num) + '/loss-model-seq2seq.pt')
            # restore full patience if obtain new minimum of the loss
            self.eval_memory_bank.early_stopping_patience = self.early_stopping_patience
        else:
            self.eval_memory_bank.early_stopping_patience = \
                max(self.eval_memory_bank.early_stopping_patience - 1, 0)  # cannot be lower than 0
        # For Validation Accuracy
        if valid_acc >= self.eval_memory_bank.best_valid_acc:
            print('\t\t Better Valid Acc! (at least equal)')
            self.eval_memory_bank.best_valid_acc = valid_acc
            self.eval_memory_bank.acc_valid_loss = valid_loss
            self.eval_memory_bank.best_valid_epoch = self.train_memory_bank.n_epoch
            self.eval_memory_bank.best_train_step = self.train_memory_bank.n_steps
            if valid_criterion == 'ACC':
                torch.save(self.model.state_dict(),
                           'experiments/exp' + str(self.train_memory_bank.exp_num) + '/acc-model-seq2seq.pt')
        print(f'\t Early Stopping Patience: '
              f'{self.eval_memory_bank.early_stopping_patience}/{self.early_stopping_patience}')
        print(f'\t Val. Loss: {valid_loss:.3f} | Val. Acc: {valid_acc:.3f} | Val. PPL: {math.exp(valid_loss):7.3f}')
        print(
            f'\t BEST. Val. Loss: {self.eval_memory_bank.best_valid_loss:.3f} | '
            f'BEST. Val. Acc: {self.eval_memory_bank.best_valid_acc:.3f} | '
            f'Val. Loss: {self.eval_memory_bank.acc_valid_loss:.3f} | '
            f'BEST. Val. Epoch: {self.eval_memory_bank.best_valid_epoch} | '
            f'BEST. Val. Step: {self.eval_memory_bank.best_train_step}')
        print("---------------------------------------\n")

    def update_aux(self, valid_acc_aux):
        if valid_acc_aux >= self.eval_memory_bank.best_valid_acc_aux:
            print('\t\t Better Valid Acc on Auxiliary Task! (at least equal)')
        print(f'\tBEST. Val. Acc Aux: {self.eval_memory_bank.best_valid_acc_aux}')

    @staticmethod
    def fix_output_n_trg(output, trg):
        """Remove first column because they are <sos> symbols
        Args:
            output: [trg len, batch size, output dim]
            trg: [trg len, batch size]
        """
        output_dim = output.shape[-1]
        output = output[1:].view(-1, output_dim)  # [(trg len - 1) * batch size, output dim]
        trg = trg[1:].view(-1)  # [(trg len - 1) * batch size]
        return output, trg

    def construct_loss_function(self):
        loss_criterion = nn.CrossEntropyLoss(ignore_index=self.args_feeder.trg_pad_idx)
        if self.task is "Multi":
            return lambda output, trg, output_aux, trg_aux: \
                (self.multi_task_ratio * loss_criterion(output, trg)) + \
                ((1 - self.multi_task_ratio) * loss_criterion(output_aux, trg_aux))
        else:
            return loss_criterion

    def train(self):

        self.model.train()
        self.model.teacher_forcing_ratio = self.tfr
        print("[Train]: Current Teacher Forcing Ratio: {:.3f}".format(self.model.teacher_forcing_ratio))

        epoch_loss = 0

        for i, batch in enumerate(self.train_iter):

            src, src_lens = getattr(batch, self.args_feeder.src_lang)
            trg, trg_lens = getattr(batch, self.args_feeder.trg_lang)
            trg_aux, trg_lens_aux = None, None

            self.optimizer.zero_grad()

            if self.task is 'Multi':
                trg_aux, trg_lens_aux = getattr(batch, self.args_feeder.auxiliary_name)
                output, output_aux = self.model(src, src_lens, trg, trg_aux)
            else:
                output, output_aux = self.model(src, src_lens, trg), None

            output, trg = self.fix_output_n_trg(output, trg)

            if self.task is 'Multi':
                output_aux, trg_aux = self.fix_output_n_trg(output_aux, trg_aux)
                loss = self.loss_function(output, trg, output_aux, trg_aux)
            else:
                loss = self.loss_function(output, trg)

            loss.backward()
            torch.nn.utils.clip_grad_norm_(self.model.parameters(), 1)  # clip = 1
            self.optimizer.step()

            epoch_loss += loss.item()
            running_loss = epoch_loss / (i + 1)

            self.train_memory_bank.n_steps += 1

            # print every ${report_interval} batches (${report_interval} steps)
            if i % self.train_memory_bank.report_interval == self.train_memory_bank.report_interval - 1:

                lr = self.args_feeder.learning_rate
                for param_group in self.optimizer.param_groups:
                    lr = param_group['lr']
                n_examples = len(self.data_container.dataset['train'].examples)
                print('[Epoch: {}][#examples: {}/{}][#steps: {}]'.format(
                    self.train_memory_bank.n_epoch,
                    (i + 1) * self.args_feeder.batch_size,
                    n_examples,
                    self.train_memory_bank.n_steps))
                print(f'\tTrain Loss: {running_loss:.3f} | '
                      f'Train PPL: {math.exp(running_loss):7.3f} '
                      f'| lr: {lr:.3e}')

                # eval the validation set for every * steps
                if (self.train_memory_bank.n_steps % (10 * self.train_memory_bank.report_interval)) == 0:
                    print('-----Val------')
                    valid_loss, valid_acc, valid_acc_aux = self.evaluate(is_test=False)
                    print('-----Tst------')
                    self.evaluate(is_test=True)

                    self.update(valid_loss, valid_acc)
                    if self.task is 'Multi':
                        self.update_aux(valid_acc_aux)
                    self.scheduler.step(valid_acc)  # scheduled on validation acc
                    self.model.train()

        return epoch_loss / len(self.train_iter)

    @staticmethod
    def matching(pred, ref, trg_field):
        tally = 0
        for j in range(pred.shape[0]):

            pred_j = pred[j, :]
            pred_j_toks = []
            for t in pred_j:
                tok = trg_field.vocab.itos[t]
                if tok == '<eos>':
                    break
                else:
                    pred_j_toks.append(tok)
            pred_j = ''.join(pred_j_toks)

            ref_j = ref[j, :]
            ref_j_toks = []
            for t in ref_j:
                tok = trg_field.vocab.itos[t]
                if tok == '<eos>':
                    break
                else:
                    ref_j_toks.append(tok)
            ref_j = ''.join(ref_j_toks)

            if pred_j == ref_j:
                tally += 1
        return tally

    def evaluate(self, is_test=False):

        self.model.eval()
        self.model.teacher_forcing_ratio = 0  # turn off teacher forcing
        print("[Eval Start]: Current Teacher Forcing Ratio: {:.3f}".format(self.model.teacher_forcing_ratio))

        epoch_loss = 0
        correct = 0
        correct_aux = 0
        iterator = self.valid_iter if not is_test else self.test_iter

        with torch.no_grad():

            for i, batch in enumerate(iterator):

                src, src_lens = getattr(batch, self.args_feeder.src_lang)
                trg, trg_lens = getattr(batch, self.args_feeder.trg_lang)
                trg_aux, trg_lens_aux = None, None

                if self.task is 'Multi':
                    trg_aux, trg_lens_aux = getattr(batch, self.args_feeder.auxiliary_name)
                    output, output_aux = self.model(src, src_lens, trg, trg_aux)
                else:
                    output, output_aux = self.model(src, src_lens, trg), None

                # ---------compute acc START----------
                pred = output[1:].argmax(2).permute(1, 0)  # [batch_size, trg_len]
                ref = trg[1:].permute(1, 0)
                correct += self.matching(pred, ref, trg_field=self.trg_field)  # match each sample
                # ---------compute acc END----------

                # ---------compute acc Pinyin START----------
                if self.task is "Multi":
                    pred_aux = output_aux[1:].argmax(2).permute(1, 0)  # [batch_size, pinyin_len]
                    ref_aux = trg_aux[1:].permute(1, 0)
                    correct_aux += self.matching(pred_aux, ref_aux,
                                                 trg_field=self.auxiliary_field)
                # ---------compute acc Pinyin END----------

                # ---------compute loss START----------
                output, trg = self.fix_output_n_trg(output, trg)

                if self.task is 'Multi':
                    output_aux, trg_aux = self.fix_output_n_trg(output_aux, trg_aux)
                    loss = self.loss_function(output, trg, output_aux, trg_aux)
                else:
                    loss = self.loss_function(output, trg)
                # ---------compute loss END----------

                epoch_loss += loss.item()

            epoch_loss = epoch_loss / len(iterator)

            n_examples = len(self.data_container.dataset['valid'].examples) if not is_test \
                else len(self.data_container.dataset['test'].examples)

            print('The number of correct predictions (main-task): {}'.format(correct))
            if self.task is 'Multi':
                print('The number of correct predictions (auxiliary-task): {}'.format(correct_aux))

            acc = correct / n_examples
            acc_aux = correct_aux / n_examples  # if single-task, then just zero

            self.model.teacher_forcing_ratio = self.tfr  # restore teacher-forcing ratio
            print("[Eval End]: Current Teacher Forcing Ratio: {:.3f}".format(self.model.teacher_forcing_ratio))

        return epoch_loss, acc, acc_aux