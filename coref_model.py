import os
import torch
import torch.nn as nn
from torch.autograd import Variable
from torch import autograd
import torch.nn.functional as F
import torch.optim as optim
import numpy as np
import random
from random import shuffle
from torch.nn.utils.rnn import pack_padded_sequence, pad_packed_sequence
import graph_results as plotter
import data_processor as parser
from allennlp.modules import FeedForward

NUM_LABELS = 3
# convention: [NEG, NULL, POS]
epochs = 20
EMBEDDING_DIM = 50
HIDDEN_DIM = 2 * EMBEDDING_DIM
NUM_POLARITIES = 6
BATCH_SIZE = 10
DROPOUT_RATE = 0.2
using_GPU = False


class Model(nn.Module):
    def __init__(self, num_labels, vocab_size, embeddings_size,
                 hidden_dim, word_embeddings, num_polarities, batch_size,
                 dropout_rate):
        super(Model, self).__init__()
        # self.dropout = nn.Dropout(dropout_rate)

        self.hidden_dim = hidden_dim
        self.batch_size = batch_size

        # Specify embedding layers
        self.word_embeds = nn.Embedding(vocab_size, embeddings_size)
        self.word_embeds.weight.data.copy_(torch.FloatTensor(word_embeddings))
        self.word_embeds.weight.requires_grad = False  # don't update the embeddings
        self.feature_embeds = nn.Embedding(num_polarities + 1, embeddings_size)  # add 1 for <pad>

        # The LSTM takes [word embeddings, feature embeddings] as inputs, and
        # outputs hidden states with dimensionality hidden_dim.
        self.lstm = nn.LSTM(2 * embeddings_size, hidden_dim, dropout=dropout_rate, batch_first=True, bidirectional=True)

        # The linear layer that maps from hidden state space to target space
        self.hidden2label = nn.Linear(2 * hidden_dim, num_labels)

        # Matrix of weights for each layer
        # Linear map from hidden layers to alpha for that layer
        #self.attention = nn.Linear(2 * hidden_dim, 1)
        # Attempting feedforward attention, using 2 layers and sigmoid activation fxn
        self.attention = FeedForward(input_dim=2*hidden_dim, num_layers=2, hidden_dims=[hidden_dim, 1],
                                     activations=nn.Sigmoid())

    def forward(self, word_vec, feature_vec, lengths=None, span_vec):
        # Apply embeddings & prepare input
        word_embeds_vec = self.word_embeds(word_vec)
        feature_embeds_vec = self.feature_embeds(feature_vec)
        # print(str(word_embeds_vec.size()) + " " + str(feature_embeds_vec.size()))

        # [word embeddings, feature embeddings]
        lstm_input = torch.cat((word_embeds_vec, feature_embeds_vec), 2)
        # print(lstm_input.size())
        #        total_length = lstm_input.size(1)  # get the max sequence length

        # Mask out padding
        if lengths is not None:
            lengths = lengths.view(-1).tolist()
            # print(lengths)
            lstm_input = pack_padded_sequence(lstm_input, lengths, batch_first=True)
            # print(lstm_input.data.size())

        # Pass through lstm
        lstm_out, _ = self.lstm(lstm_input)

        if lengths is not None:
            lstm_out = pad_packed_sequence(lstm_out, batch_first=True)[0]
            # print(lstm_out)
            # if you visualize the output, padding is all 0, so can unpack now and weighted padding is 0,
            # contributing 0 to weighted_lstm_out

        dimension = 1
        # Compute and apply weights (attention) to each layer (so dim=1)
        alphas = self.attention(lstm_out)
        alphas = F.softmax(alphas, dim=dimension)  # batch_size x num_layers x 1
        weighted_lstm_out = torch.sum(torch.mul(alphas, lstm_out), dim=dimension)  # batch_size x hidden_dim
        '''
        weighted_lstm_out = lstm_out[-1]
        '''
        # Get final results, passing in weighted lstm output:
        tag_space = self.hidden2label(weighted_lstm_out)  # batch_size x 1
        log_probs = F.log_softmax(tag_space, dim=dimension)  # batch_size x 1

        return log_probs, alphas


def train(Xtrain, Xdev, Xtest,
          model, word_to_ix, ix_to_word,
          using_GPU, lr_decay=1):
    print("Evaluating before training...")
    train_res = []
    dev_res = []
    dev_f1_aves = []
    test_res = []
    train_accs = []
    dev_accs = []
    test_accs = []

    '''
    # Just for 1 batch
    for batch in Xtrain:
        plotter.graph_attention(model, word_to_ix, ix_to_word, batch, using_GPU)
        break
    '''
    print("evaluating training...")
    train_score, train_acc = evaluate(model, word_to_ix, ix_to_word, Xtrain, using_GPU)
    print("train f1 scores = " + str(train_score))
    dev_score, dev_acc = evaluate(model, word_to_ix, ix_to_word, Xdev, using_GPU)
    print("dev f1 scores = " + str(dev_score))
    train_res.append(train_score)
    dev_res.append(dev_score)
    dev_f1_aves.append(sum(dev_score) / len(dev_score))
    best_epoch = 0
    train_accs.append(train_acc)
    dev_accs.append(dev_acc)

    test_score, test_acc = evaluate(model, word_to_ix, ix_to_word, Xtest, using_GPU)
    test_res.append(test_score)
    test_accs.append(test_acc)

    loss_function = nn.NLLLoss()
    losses_epoch = []

    # skip updating the non-requires-grad params (i.e. the embeddings)
    optimizer = optim.Adam(filter(lambda p: p.requires_grad, model.parameters()), lr=0.01, weight_decay=0)

    for epoch in range(0, epochs):
        losses = []
        print("Epoch " + str(epoch))
        i = 0
        for batch in Xtrain:
            (words, lengths), polarity, label = batch.text, batch.polarity, batch.label

            # Step 1. Remember that Pytorch accumulates gradients.
            # We need to clear them out before each instance
            model.zero_grad()
            model.batch_size = len(label.data)  # set batch size

            # Step 3. Run our forward pass.
            log_probs, alphas = model(words, polarity, lengths)

            # Step 4. Compute the loss, gradients, and update the parameters by
            # calling optimizer.step()
            loss = loss_function(log_probs, label)  # log_probs = actual distr, target = computed distr
            losses.append(float(loss))
            loss.backward()
            optimizer.step()
            # print("loss = " + str(loss))
            if (i % 10 == 0):
                print("    " + str(i))
            i += 1
        print("loss = " + str((sum(losses) / len(losses))))
        losses_epoch.append(float(sum(losses)) / float(len(losses)))
        # Apply decay
        if (epoch % 10 == 0):
            for param_group in optimizer.param_groups:
                param_group['lr'] *= lr_decay
        '''
        # Just for 1 batch
        for batch in Xtrain:
            plotter_graph_attention(model, word_to_ix, ix_to_word, batch, using_GPU)
            break
        '''
        print("Evaluating...")
        print("evaluating training...")
        train_score, train_acc = evaluate(model, word_to_ix, ix_to_word, Xtrain, using_GPU)
        print("train f1 scores = " + str(train_score))
        print(Xdev)
        dev_score, dev_acc = evaluate(model, word_to_ix, ix_to_word, Xdev, using_GPU)
        print("dev f1 scores = " + str(dev_score))
        train_res.append(train_score)
        train_accs.append(train_acc)
        dev_res.append(dev_score)
        dev_f1_aves.append(sum(dev_score) / len(dev_score))
        if (dev_f1_aves[epoch] > dev_f1_aves[best_epoch]):
            best_epoch = epoch
            print("Updated best epoch: " + str(dev_f1_aves[best_epoch]))
        dev_accs.append(dev_acc)
        test_score, test_acc = evaluate(model, word_to_ix, ix_to_word, Xtest, using_GPU)
        test_res.append(test_score)
        test_accs.append(test_acc)
    return train_res, dev_res, test_res, train_accs, dev_accs, test_accs, losses_epoch, best_epoch


def decode(word_indices, ix_to_word):
    words = [ix_to_word[index] for index in word_indices.data]
    return words


def evaluate(model, word_to_ix, ix_to_word, Xs, using_GPU):
    # Set model to eval mode to turn off dropout.
    model.eval()

    total_true = [0, 0, 0]
    total_pred = [0, 0, 0]
    total_correct = [0, 0, 0]

    num_examples = 0
    num_correct = 0

    print("Iterate across : " + str(len(Xs)) + " batch(es)")
    counter = 0
    # count positive classifications in pos
    for batch in Xs:
        counter += 1
        # print(word_to_ix)
        (words, lengths), polarity, label = batch.text, batch.polarity, batch.label
        words.volatile = True
        lengths.volatile = True
        polarity.volatile = True
        label.volatile = True

        model.batch_size = len(label.data)  # set batch size
        '''
        if len(label.data) > BATCH_SIZE:
            print(label.data)
        '''
        log_probs, attention = model(words, polarity, lengths)  # log probs: batch_size x 3
        pred_label = log_probs.data.max(1)[1]

        # Count the number of examples in this batch
        for i in range(0, NUM_LABELS):
            total_true[i] += torch.sum(label.data == i)
            total_pred[i] += torch.sum(pred_label == i)
            total_correct[i] += torch.sum((pred_label == i) * (label.data == i))
            '''
            print(i)
            print(label.data)
            print(pred_label)
            print(total_correct)
            '''
        num_correct += float(torch.sum(pred_label == label.data))
        num_examples += len(label.data)

        assert sum(total_true) == num_examples
        assert sum(total_pred) == num_examples
        assert sum(total_correct) == num_correct

        if counter % 10 == 0:
            print(counter)

    # Compute f1 scores (separate method?)
    precision = [0, 0, 0]
    recall = [0, 0, 0]
    f1 = [0, 0, 0]
    for i in range(0, NUM_LABELS):
        if total_pred[i] == 0:
            precision[i] = 0.0
        else:
            precision[i] = total_correct[i] / total_pred[i]
        recall[i] = total_correct[i] / total_true[i]
        if precision[i] + recall[i] == 0:
            f1[i] = 0.0
        else:
            f1[i] = 2 * (precision[i] * recall[i]) / (precision[i] + recall[i])

    # Compute accuracy
    accuracy = num_correct / float(num_examples)
    print(accuracy)

    print(f1)
    #    score = f1_score(list(predictions), list(truths), labels=[0, 1, 2], average=None)
    #    print(score)

    # Set the model back to train mode, to reactivate dropout.
    model.train()

    return f1, accuracy


def main():
    train_data, dev_data, test_data, TEXT, DOCID = parser.parse_input_files(BATCH_SIZE, EMBEDDING_DIM, using_GPU)

    word_to_ix = TEXT.vocab.stoi
    ix_to_word = TEXT.vocab.itos

    VOCAB_SIZE = len(word_to_ix)

    word_embeds = TEXT.vocab.vectors

    model = Model(NUM_LABELS, VOCAB_SIZE,
                  EMBEDDING_DIM, HIDDEN_DIM, word_embeds,
                  NUM_POLARITIES, BATCH_SIZE, DROPOUT_RATE)

    # Move the model to the GPU if available
    if using_GPU:
        model = model.cuda()

    train_c, dev_c, test_c, train_a, dev_a, test_a, losses, best_epoch = \
        train(train_data, dev_data, test_data,
              model,
              word_to_ix, ix_to_word,
              using_GPU)
    print("Train results: ")
    print("    " + str(train_c))
    print("    " + str(train_a))
    print("Dev results: ")
    print("    " + str(dev_c))
    print("    " + str(dev_a))
    print("Test results: ")
    print("    " + str(test_c))
    print("    " + str(test_a))
    print("Losses: ")
    print(losses)

    print("Best epoch = " + str(best_epoch))
    print("Train results: ")
    print("    " + str(train_c[best_epoch]) + " " + str(sum(train_c[best_epoch]) / len(train_c[best_epoch])))
    print("    " + str(train_a[best_epoch]))
    print("Dev results: ")
    print("    " + str(dev_c[best_epoch]) + " " + str(sum(dev_c[best_epoch]) / len(dev_c[best_epoch])))
    print("    " + str(dev_a[best_epoch]))
    print("Test results: ")
    print("    " + str(test_c[best_epoch]) + " " + str(sum(test_c[best_epoch]) / len(test_c[best_epoch])))
    print("    " + str(test_a[best_epoch]))

    '''                   
    print(str(dev_c))
    best_epochs = np.argmax(np.array(dev_c))
    dev_results = dev_c[best_epochs]
    print("Test performance = " + str(test_c[best_epochs]))
    '''


if __name__ == "__main__":
    main()
