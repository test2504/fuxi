#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author  : jeffzhang
# @Time    : 2019/1/22
# @File    : poc_task.py
# @Desc    : ""

import time
from tempfile import gettempdir
from bson import ObjectId
from datetime import datetime
from fuxi.libs.common.pocsuite_api import pocsuite_scanner
from fuxi import fuxi_celery
from fuxi.libs.common.target_parser import target_parse
from fuxi.database import MongoDB, T_POC_VULS, T_POC_TASKS, T_POC_PLUGINS
from fuxi.libs.common.logger import logger


def poc_config_init(target_list, poc_str, threat=10, quiet=True):
    _poc_config = {
        'url_file': '',
        'poc': '',
        'threads': threat,
        'quiet': quiet
    }
    tmp_path = gettempdir()
    try:
        target_file_path = tmp_path + "/target_{}".format(int(time.time()))
        with open(target_file_path, 'w') as _target_f_save:
            _target_f_save.write("\n".join(target_list))
        _poc_config['url_file'] = target_file_path
    except Exception as e:
        logger.error("save target to temp file failed: {} {}".format(target_list[0], e))
    try:
        poc_file_path = tmp_path + "/poc_{}.py".format(int(time.time()))
        with open(poc_file_path, 'w') as _poc_f_save:
            _poc_f_save.write(poc_str.encode('ascii', 'ignore').decode('ascii'))
        _poc_config['poc'] = poc_file_path
    except Exception as e:
        logger.error("save poc to temp file failed: {}".format(e))
    return _poc_config


@fuxi_celery.task()
def t_poc_scanner(task_id, thread):
    """
    所以 celery 任务 以 t_ 开头用于标识 并在 celery_worker.py 中引入
    接收 task_id 及并发数 然后使用 task_id 数据库获取target
    :param task_id: poc task id（写入数据库时返回）
    :param thread: 扫描并发
    :return: 不返回 直接写结果入库
    """
    try:
        t_item = MongoDB(T_POC_TASKS).find_one({"_id": ObjectId(task_id)})
        task_name = t_item['name']
        target_list = target_parse(t_item['target'].split("\n"))
        poc_id_list = t_item['poc_id'].split("\n")
        # 删除旧结果
        MongoDB(T_POC_VULS).delete_many({"t_id": task_id})
        # 扫描开始前更改任务状态 running
        MongoDB(T_POC_TASKS).update_one({"_id": ObjectId(task_id)}, {"status": "running"})
        count = 0
        logger.success("{} poc task running".format(task_id))
        for poc_id in poc_id_list:
            # 通过插件 id 查询 poc
            poc_item = MongoDB(T_POC_PLUGINS).find_one({"_id": ObjectId(poc_id)})
            # 有时候插件删除了 但任务里还有这个插件的任务 扫描会报错 这个做个判断
            if not poc_item:
                continue
            _poc_config = poc_config_init(target_list, poc_item['poc'], thread)
            _scan_items = pocsuite_scanner(_poc_config)
            for _item in _scan_items:
                try:
                    vul_data = {
                        "t_id": task_id,
                        "poc_id": poc_id,
                        "task_name": task_name,
                        "poc_name": poc_item['name'],
                        "status": _item['status'],
                        "target": _item['target'],
                        "app": poc_item['app'] if poc_item['app'] else "-",
                        "result": _item['result'] if _item['result'] else _item['error_msg'][1],
                        "date": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
                    }
                    MongoDB(T_POC_VULS).insert_one(vul_data)
                    if _item['status'] == "success":
                        count += 1
                except Exception as e:
                    logger.success("save poc scan result failed: {}".format(e))
        # 扫描完成后更改任务信息
        MongoDB(T_POC_TASKS).update_one({"_id": ObjectId(task_id)}, {
            "status": "completed",
            "vul_count": count,
            "end_date": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
        })
        logger.success("{} poc task completed".format(task_id))
    except Exception as e:
        logger.error("{} poc task failed: {}".format(task_id, e))


def quick_poc_scanner(target_list, poc_id_list):
    target_list = target_parse(target_list)
    result = []
    try:
        for poc_id in poc_id_list:
            # 通过插件 id 查询 poc
            poc_item = MongoDB(T_POC_PLUGINS).find_one({"_id": ObjectId(poc_id)})
            _poc_config = poc_config_init(target_list, poc_item['poc'])
            _tmp_result = pocsuite_scanner(_poc_config)
            for _item in _tmp_result:
                result.append(_item)
    except Exception as e:
        logger.error("{} quick poc scan failed: {}".format(target_list[0], e))
    return result


@fuxi_celery.task()
def schedule_poc_scanner():
    task_items = MongoDB(T_POC_TASKS).find()
    for item in task_items:
        t_id = str(item['_id'])
        thread = item['thread']
        freq = item['freq']
        end_date = item['end_date']
        status = item['status']
        if freq == 'daily':
            if "completed" in status:
                start_date = datetime.strptime(end_date, "%Y-%m-%d %H:%M:%S")
                plan_time = (datetime.now() - start_date).total_seconds()
                if plan_time > 60 * 60 * 24:
                    logger.info("daily task running: poc scan {}".format(t_id))
                    t_poc_scanner.delay(t_id, thread)
                    logger.info("daily task completed: poc scan {}".format(t_id))

        elif freq == 'weekly':
            if "completed" in status:
                start_date = datetime.strptime(end_date, "%Y-%m-%d %H:%M:%S")
                plan_time = (datetime.now() - start_date).total_seconds()
                if plan_time > 60 * 60 * 24 * 7:
                    logger.info("weekly task running: poc scan {}".format(t_id))
                    t_poc_scanner.delay(t_id, thread)
                    logger.info("weekly task completed: poc scan {}".format(t_id))

        elif freq == 'monthly':
            if "completed" in status:
                start_date = datetime.strptime(end_date, "%Y-%m-%d %H:%M:%S")
                plan_time = (datetime.now() - start_date).total_seconds()
                if plan_time > 60 * 60 * 24 * 30:
                    logger.info("monthly task running: poc scan {}".format(t_id))
                    t_poc_scanner.delay(t_id, thread)
                    logger.info("monthly task completed: poc scan {}".format(t_id))


