import tensorflow as tf
import ujson as json
import numpy as np
from tqdm import tqdm
import os

from model import Model
from util import *


def train(config):
    """
    训练与验证函数
    """
    with open(config.id2vec_file, "r") as fh:
        id2vec = np.array(json.load(fh), dtype=np.float32)
    with open(config.train_eval_file, "r") as fh:
        train_eval_file = json.load(fh)
    with open(config.dev_eval_file, "r") as fh:
        dev_eval_file = json.load(fh)
    with open(config.dev_meta, "r") as fh:
        meta = json.load(fh)

    dev_total = meta["total"]  # 测试集数据量

    print("Building model...")
    parser = get_record_parser(config)
    train_dataset = get_batch_dataset(config.train_record_file, parser, config)
    dev_dataset = get_dataset(config.dev_record_file, parser, config)

    # 可馈送迭代器，通过feed_dict机制选择每次sess.run时调用train_iterator还是dev_iterator
    handle = tf.placeholder(tf.string, shape=[])
    iterator = tf.data.Iterator.from_string_handle(
        handle, train_dataset.output_types, train_dataset.output_shapes)
    train_iterator = train_dataset.make_one_shot_iterator()
    dev_iterator = dev_dataset.make_one_shot_iterator()

    model = Model(config, iterator, id2vec)

    sess_config = tf.ConfigProto(allow_soft_placement=True)
    sess_config.gpu_options.allow_growth = True

    loss_save = 100.0
    patience = 0
    lr = config.init_learning_rate

    with tf.Session(config=sess_config) as sess:
        writer = tf.summary.FileWriter(config.log_dir)
        sess.run(tf.global_variables_initializer())
        saver = tf.train.Saver()
        train_handle = sess.run(train_iterator.string_handle())
        dev_handle = sess.run(dev_iterator.string_handle())
        sess.run(tf.assign(model.is_train, tf.constant(True, dtype=tf.bool)))
        sess.run(tf.assign(model.learning_rate,
                           tf.constant(lr, dtype=tf.float32)))

        for _ in tqdm(range(1, config.num_steps + 1)):
            global_step = sess.run(model.global_step) + 1
            loss, train_op = sess.run([model.loss, model.train_op], feed_dict={
                                      handle: train_handle})
            if global_step % config.period == 0:  # 每隔一段步数就记录一次train_loss
                loss_sum = tf.Summary(value=[tf.Summary.Value(
                    tag="model/loss", simple_value=loss), ])
                writer.add_summary(loss_sum, global_step)
            if global_step % config.checkpoint == 0:
                sess.run(tf.assign(model.is_train,
                                   tf.constant(False, dtype=tf.bool)))
            # 评估训练集
                _, summ = evaluate_batch(
                    model, config.val_num_batches, train_eval_file, sess, "train_eval", handle, train_handle)
                for s in summ:
                    writer.add_summary(s, global_step)

                # 评估验证集
                metrics, summ = evaluate_batch(
                    model, dev_total // config.batch_size + 1, dev_eval_file, sess, "dev", handle, dev_handle)
                sess.run(tf.assign(model.is_train,
                                   tf.constant(True, dtype=tf.bool)))

                # 学习率衰减的策略
                dev_loss = metrics["loss"]
                if dev_loss < loss_save:
                    loss_save = dev_loss
                    patience = 0
                else:
                    patience += 1
                if patience >= config.patience:
                    lr /= 2.0
                    loss_save = dev_loss
                    patience = 0
                sess.run(tf.assign(model.learning_rate,
                                   tf.constant(lr, dtype=tf.float32)))
                for s in summ:
                    writer.add_summary(s, global_step)
                writer.flush()  # 将事件文件刷新到磁盘
                filename = os.path.join(
                    config.save_dir, "model_{}.ckpt".format(global_step))
                saver.save(sess, filename)


def evaluate_batch(model, num_batches, eval_file, sess, data_type, handle, str_handle):
    """
    模型评估函数,需要重写
    """
    answer_dict = {}  # 答案词典
    losses = []
    for _ in tqdm(range(1, num_batches + 1)):
        qa_id, loss, answer = sess.run(
            [model.qa_id, model.loss, model.classes], feed_dict={handle: str_handle})
        answer_dict_ = {}
        for ids, ans in zip(qa_id, answer):
            answer_dict_[str(ids)] = ans
        answer_dict.update(answer_dict_)
        losses.append(loss)
    loss = np.mean(losses)
    metrics = evaluate(eval_file, answer_dict)
    metrics["loss"] = loss
    loss_sum = tf.Summary(value=[tf.Summary.Value(
        tag="{}/loss".format(data_type), simple_value=metrics["loss"]), ])
    acc_sum = tf.Summary(value=[tf.Summary.Value(
        tag="{}/accuracy".format(data_type), simple_value=metrics["accuracy"]), ])
    return metrics, [loss_sum, acc_sum]


def test(config):
    """
    测试函数
    """
    with open(config.id2vec_file, "r") as fh:
        id2vec = np.array(json.load(fh), dtype=np.float32)
    with open(config.test_eval_file, "r") as fh:
        eval_file = json.load(fh)
    with open(config.test_meta, "r") as fh:
        meta = json.load(fh)

    total = meta["total"]

    print("Loading model...")
    test_batch = get_dataset(config.test_record_file, get_record_parser(
        config), config).make_one_shot_iterator()

    model = Model(config, test_batch, id2vec, trainable=False)

    sess_config = tf.ConfigProto(allow_soft_placement=True)
    sess_config.gpu_options.per_process_gpu_memory_fraction = 0.9
    sess_config.gpu_options.allow_growth = True

    with tf.Session(config=sess_config) as sess:
        sess.run(tf.global_variables_initializer())
        saver = tf.train.Saver()
        saver.restore(sess, tf.train.latest_checkpoint(config.save_dir))
        sess.run(tf.assign(model.is_train, tf.constant(False, dtype=tf.bool)))
        losses = []
        answer_dict = {}
        for step in tqdm(range(total // config.batch_size + 1)):
            qa_id, answer = sess.run([model.qa_id, model.classes])
            answer_dict_ = {}
            for ids, ans in zip(qa_id, answer):
                answer_dict_[str(ids)] = ans
            answer_dict.update(answer_dict_)
            losses.append(loss)
        loss = np.mean(losses)
        # 这里还要加一个写文件的操作
