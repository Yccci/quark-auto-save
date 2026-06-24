# !/usr/bin/env python3
# -*- coding: utf-8 -*-
from flask import (
    json,
    Flask,
    url_for,
    session,
    jsonify,
    request,
    redirect,
    Response,
    render_template,
    send_from_directory,
    stream_with_context,
)
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from concurrent.futures import ThreadPoolExecutor, as_completed
from sdk.cloudsaver import CloudSaver
from sdk.pansou import PanSou
from datetime import timedelta
import subprocess
import requests
import hashlib
import logging
import traceback
import base64
import sys
import os
import re

parent_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, parent_dir)
from quark_auto_save import (
    Quark,
    Config,
    MagicRename,
    calc_share_total_size,
    calc_share_total_size_deep,
)

print(
    r"""
   ____    ___   _____
  / __ \  /   | / ___/
 / / / / / /| | \__ \
/ /_/ / / ___ |___/ /
\___\_\/_/  |_/____/

-- Quark-Auto-Save --
 """
)
sys.stdout.flush()


def get_app_ver():
    """获取应用版本"""
    try:
        with open("build.json", "r") as f:
            build_info = json.loads(f.read())
            BUILD_SHA = build_info["BUILD_SHA"]
            BUILD_TAG = build_info["BUILD_TAG"]
    except Exception as e:
        BUILD_SHA = os.getenv("BUILD_SHA", "")
        BUILD_TAG = os.getenv("BUILD_TAG", "")
    if BUILD_TAG[:1] == "v":
        return BUILD_TAG
    elif BUILD_SHA:
        return f"{BUILD_TAG}({BUILD_SHA[:7]})"
    else:
        return "dev"


# 文件路径
PYTHON_PATH = "python3" if os.path.exists("/usr/bin/python3") else "python"
SCRIPT_PATH = os.environ.get("SCRIPT_PATH", "./quark_auto_save.py")
CONFIG_PATH = os.environ.get("CONFIG_PATH", "./config/quark_config.json")
PLUGIN_FLAGS = os.environ.get("PLUGIN_FLAGS", "")
DEBUG = os.environ.get("DEBUG", "false").lower() == "true"
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = os.environ.get("PORT", 5005)
TASK_TIMEOUT = int(os.environ.get("TASK_TIMEOUT", 1800))

config_data = {}
task_plugins_config_default = {}

app = Flask(__name__)
app.config["APP_VERSION"] = get_app_ver()
app.secret_key = "ca943f6db6dd34823d36ab08d8d6f65d"
app.config["SESSION_COOKIE_NAME"] = "QUARK_AUTO_SAVE_SESSION"
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=31)
app.json.ensure_ascii = False
app.json.sort_keys = False
app.jinja_env.variable_start_string = "[["
app.jinja_env.variable_end_string = "]]"

scheduler = BackgroundScheduler()
logging.basicConfig(
    level=logging.DEBUG if DEBUG else logging.INFO,
    format="[%(asctime)s][%(levelname)s] %(message)s",
    datefmt="%m-%d %H:%M:%S",
)
# 过滤werkzeug日志输出
if not DEBUG:
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    logging.getLogger("apscheduler").setLevel(logging.ERROR)
    sys.modules["flask.cli"].show_server_banner = lambda *x: None


def gen_md5(string):
    md5 = hashlib.md5()
    md5.update(string.encode("utf-8"))
    return md5.hexdigest()


def merge_tasklist(disk_tasklist, client_tasklist):
    """合并任务列表，避免未加载完整配置时覆盖已有任务。"""
    if client_tasklist is None:
        return disk_tasklist
    if not disk_tasklist:
        return client_tasklist
    if len(client_tasklist) >= len(disk_tasklist):
        return client_tasklist

    disk_names = {t.get("taskname") for t in disk_tasklist}
    client_names = {t.get("taskname") for t in client_tasklist}
    if not client_names:
        return client_tasklist
    if client_names.isdisjoint(disk_names):
        logging.warning(">>> 检测到任务列表疑似未包含已有任务，已自动合并而非覆盖")
        return disk_tasklist + client_tasklist
    return client_tasklist


def reload_config_data():
    """从磁盘重新加载配置，并保留内存中的 WebUI 账号信息。"""
    global config_data
    disk_data = Config.read_json(CONFIG_PATH)
    disk_data["webui"] = config_data.get("webui") or disk_data.get("webui", {})
    config_data = disk_data
    return config_data


def get_login_token():
    username = config_data["webui"]["username"]
    password = config_data["webui"]["password"]
    return gen_md5(f"token{username}{password}+-*/")[8:24]


def is_login():
    login_token = get_login_token()
    if session.get("token") == login_token or request.args.get("token") == login_token:
        return True
    else:
        return False


# 设置icon
@app.route("/favicon.ico")
def favicon():
    return send_from_directory(
        os.path.join(app.root_path, "static"),
        "favicon.ico",
        mimetype="image/vnd.microsoft.icon",
    )


# 登录页面
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = config_data["webui"]["username"]
        password = config_data["webui"]["password"]
        # 验证用户名和密码
        if (username == request.form.get("username")) and (
            password == request.form.get("password")
        ):
            logging.info(f">>> 用户 {username} 登录成功")
            session.permanent = True
            session["token"] = get_login_token()
            return redirect(url_for("index"))
        else:
            logging.info(f">>> 用户 {username} 登录失败")
            return render_template("login.html", message="登录失败")

    if is_login():
        return redirect(url_for("index"))
    return render_template("login.html", error=None)


# 退出登录
@app.route("/logout")
def logout():
    session.pop("token", None)
    return redirect(url_for("login"))


# 管理页面
@app.route("/")
def index():
    if not is_login():
        return redirect(url_for("login"))
    return render_template(
        "index.html", version=app.config["APP_VERSION"], plugin_flags=PLUGIN_FLAGS
    )


# 获取配置数据
@app.route("/data")
def get_data():
    if not is_login():
        return jsonify({"success": False, "message": "未登录"})
    data = Config.read_json(CONFIG_PATH)
    del data["webui"]
    data["api_token"] = get_login_token()
    data["task_plugins_config_default"] = task_plugins_config_default
    return jsonify({"success": True, "data": data})


# 更新数据
@app.route("/update", methods=["POST"])
def update():
    global config_data
    if not is_login():
        return jsonify({"success": False, "message": "未登录"})
    reload_config_data()
    # 使用允许列表防止批量赋值攻击
    allowed_keys = ["cookie", "crontab", "push_config", "tasklist",
                    "magic_regex", "plugins", "source"]
    for key, value in request.json.items():
        if key in allowed_keys:
            if key == "tasklist":
                value = merge_tasklist(config_data.get("tasklist", []), value)
            config_data.update({key: value})
    Config.write_json(CONFIG_PATH, config_data)
    # 重新加载任务
    if reload_tasks():
        logging.info(f">>> 配置更新成功")
        return jsonify({"success": True, "message": "配置更新成功"})
    else:
        logging.info(f">>> 配置更新失败")
        return jsonify({"success": False, "message": "配置更新失败"})


# 处理运行脚本请求
@app.route("/run_script_now", methods=["POST"])
def run_script_now():
    if not is_login():
        return jsonify({"success": False, "message": "未登录"})
    tasklist = request.json.get("tasklist", [])
    command = [PYTHON_PATH, "-u", SCRIPT_PATH, CONFIG_PATH]
    logging.info(
        f">>> 手动运行任务 [{tasklist[0].get('taskname') if len(tasklist)>0 else 'ALL'}] 开始执行..."
    )

    def generate_output():
        # 设置环境变量
        process_env = os.environ.copy()
        process_env["PYTHONIOENCODING"] = "utf-8"
        if request.json.get("quark_test"):
            process_env["QUARK_TEST"] = "true"
            process_env["COOKIE"] = json.dumps(
                request.json.get("cookie", []), ensure_ascii=False
            )
            process_env["PUSH_CONFIG"] = json.dumps(
                request.json.get("push_config", {}), ensure_ascii=False
            )
        if tasklist:
            process_env["TASKLIST"] = json.dumps(tasklist, ensure_ascii=False)
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=process_env,
        )
        try:
            for line in iter(process.stdout.readline, ""):
                logging.info(line.strip())
                yield f"data: {line}\n\n"
            yield "data: [DONE]\n\n"
        finally:
            process.stdout.close()
            process.wait()

    return Response(
        stream_with_context(generate_output()),
        content_type="text/event-stream;charset=utf-8",
    )


@app.route("/task_suggestions")
def get_task_suggestions():
    if not is_login():
        return jsonify({"success": False, "message": "未登录"})
    query = request.args.get("q", "").lower()
    deep = request.args.get("d", "").lower()
    try:
        results = _collect_task_suggestions(query, deep)
        return jsonify({"success": True, "data": results})
    except Exception as e:
        return jsonify({"success": True, "message": f"error: {str(e)}"})


@app.route("/task_suggestions_stream")
def get_task_suggestions_stream():
    if not is_login():
        return jsonify({"success": False, "message": "未登录"})
    query = request.args.get("q", "").lower()
    deep = request.args.get("d", "").lower()

    def generate():
        sources = _get_task_suggestion_sources(query, deep)
        total = len(sources)
        if total == 0:
            yield _sse_event({"type": "done", "success": True, "data": []})
            return

        yield _sse_event(
            {
                "type": "start",
                "total": total,
                "sources": [name for name, _ in sources],
            }
        )

        search_results = []
        completed = 0
        completed_sources = []
        all_source_names = [name for name, _ in sources]
        with ThreadPoolExecutor(max_workers=total) as executor:
            future_map = {}
            for name, search_fn in sources:
                future_map[executor.submit(search_fn)] = name
                pending_sources = [
                    source_name
                    for source_name in all_source_names
                    if source_name not in completed_sources
                ]
                yield _sse_event(
                    {
                        "type": "searching",
                        "source": name,
                        "completed": completed,
                        "total": total,
                        "pending_sources": pending_sources,
                        "completed_sources": list(completed_sources),
                    }
                )

            for future in as_completed(future_map):
                name = future_map[future]
                try:
                    result = future.result() or []
                except Exception as e:
                    logging.warning(f"搜索源 [{name}] 失败: {e}")
                    result = []
                search_results.extend(result)
                completed += 1
                completed_sources.append(name)
                pending_sources = [
                    source_name
                    for source_name in all_source_names
                    if source_name not in completed_sources
                ]
                yield _sse_event(
                    {
                        "type": "progress",
                        "source": name,
                        "completed": completed,
                        "total": total,
                        "count": len(result),
                        "found": len(search_results),
                        "pending_sources": pending_sources,
                        "completed_sources": list(completed_sources),
                    }
                )

        results = _dedupe_task_suggestions(search_results)
        yield _sse_event({"type": "done", "success": True, "data": results})

    return Response(
        stream_with_context(generate()),
        content_type="text/event-stream;charset=utf-8",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _sse_event(payload):
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _is_source_enabled(source_data, default=True):
    """判断搜索源是否启用，未配置时沿用默认值。"""
    if not isinstance(source_data, dict):
        return default
    enable = source_data.get("enable")
    if enable is None or enable == "":
        return default
    if isinstance(enable, bool):
        return enable
    return str(enable).lower() not in ("false", "0", "no", "off")


def _get_task_suggestion_sources(query, deep):
    net_data = config_data.get("source", {}).get("net", {})
    cs_data = config_data.get("source", {}).get("cloudsaver", {})
    ps_data = config_data.get("source", {}).get("pansou", {})
    sources = []

    def net_search():
        if str(net_data.get("enable", "true")).lower() != "false":
            base_url = base64.b64decode("aHR0cHM6Ly9zLjkxNzc4OC54eXo=").decode()
            url = f"{base_url}/task_suggestions?q={query}&d={deep}"
            response = requests.get(url, timeout=30)
            return response.json()
        return []

    def cs_search():
        if (
            cs_data.get("server")
            and cs_data.get("username")
            and cs_data.get("password")
        ):
            cs = CloudSaver(cs_data.get("server"))
            cs.set_auth(
                cs_data.get("username", ""),
                cs_data.get("password", ""),
                cs_data.get("token", ""),
            )
            search = cs.auto_login_search(query)
            if search.get("success"):
                if search.get("new_token"):
                    reload_config_data()
                    config_data.setdefault("source", {}).setdefault(
                        "cloudsaver", {}
                    )["token"] = search.get("new_token")
                    Config.write_json(CONFIG_PATH, config_data)
                return cs.clean_search_results(search.get("data"))
        return []

    def ps_search():
        if ps_data.get("server"):
            ps = PanSou(ps_data.get("server"))
            return ps.search(query, deep == "1", timeout=45)
        return []

    if _is_source_enabled(net_data):
        sources.append(("网络搜索", net_search))
    if (
        _is_source_enabled(cs_data)
        and cs_data.get("server")
        and cs_data.get("username")
        and cs_data.get("password")
    ):
        sources.append(("CloudSaver", cs_search))
    if _is_source_enabled(ps_data) and ps_data.get("server"):
        sources.append(("PanSou", ps_search))
    return sources


def _dedupe_task_suggestions(search_results):
    results = []
    link_array = []
    search_results.sort(key=lambda x: x.get("datetime", ""), reverse=True)
    for item in search_results:
        url = item.get("shareurl", "")
        if url != "" and url not in link_array:
            link_array.append(url)
            results.append(item)
    return results


def _collect_task_suggestions(query, deep):
    search_results = []
    sources = _get_task_suggestion_sources(query, deep)
    if not sources:
        return []
    with ThreadPoolExecutor(max_workers=len(sources)) as executor:
        futures = [executor.submit(search_fn) for _, search_fn in sources]
        for future in as_completed(futures):
            result = future.result() or []
            search_results.extend(result)
    return _dedupe_task_suggestions(search_results)


@app.route("/get_share_detail", methods=["POST"])
def get_share_detail():
    if not is_login():
        return jsonify({"success": False, "message": "未登录"})
    shareurl = request.json.get("shareurl", "")
    stoken = request.json.get("stoken", "")
    account = Quark()
    pwd_id, passcode, pdir_fid, paths = account.extract_url(shareurl)
    if not stoken:
        get_stoken = account.get_stoken(pwd_id, passcode)
        if get_stoken.get("status") == 200:
            stoken = get_stoken["data"]["stoken"]
        else:
            return jsonify(
                {"success": False, "data": {"error": get_stoken.get("message")}}
            )
    share_detail = account.get_detail(
        pwd_id, stoken, pdir_fid, _fetch_share=1, fetch_share_full_path=1
    )

    if share_detail.get("code") != 0:
        return jsonify(
            {"success": False, "data": {"error": share_detail.get("message")}}
        )

    data = share_detail["data"]
    data["paths"] = [
        {"fid": i["fid"], "name": i["file_name"]}
        for i in share_detail["data"].get("full_path", [])
    ] or paths
    data["stoken"] = stoken

    # 确保文件夹 include_items 字段可用（参考 x1ao4）
    if isinstance(data.get("list"), list):
        for file_item in data["list"]:
            if file_item.get("dir"):
                include_items = file_item.get("include_items")
                if include_items is None:
                    file_item["include_items"] = 0
                else:
                    try:
                        file_item["include_items"] = int(include_items)
                    except (ValueError, TypeError):
                        file_item["include_items"] = 0

    if request.json.get("validate_only"):
        data["total_size"] = calc_share_total_size(data.get("list"))
    else:
        data["total_size"] = calc_share_total_size_deep(
            account, pwd_id, stoken, data.get("list")
        )

    # 过滤 01x.mp4 类型无效视频格式
    if os.getenv("FILTER_INVALID_VIDEO", "true") == "true":
        for share_file in data["list"]:
            if (
                share_file["file_name"].lower().endswith((".mp4", ".mkv"))
                and not share_file["dir"]
                and share_file["obj_category"] != "video"
            ):
                return jsonify({"success": False, "data": {"error": "无效视频格式"}})

    # 正则处理预览
    def preview_regex(data):
        task = request.json.get("task", {})
        magic_regex = request.json.get("magic_regex", {})
        mr = MagicRename(magic_regex)
        mr.set_taskname(task.get("taskname", ""))
        account = Quark(config_data["cookie"][0])
        get_fids = account.get_fids([task.get("savepath", "")])
        if get_fids:
            dir_file_list = account.ls_dir(get_fids[0]["fid"])["data"]["list"]
            dir_filename_list = [dir_file["file_name"] for dir_file in dir_file_list]
        else:
            dir_file_list = []
            dir_filename_list = []

        pattern, replace = mr.magic_regex_conv(
            task.get("pattern", ""), task.get("replace", "")
        )
        for share_file in data["list"]:
            search_pattern = (
                task["update_subdir"]
                if share_file["dir"] and task.get("update_subdir")
                else pattern
            )
            if re.search(search_pattern, share_file["file_name"]):
                # 文件名重命名，目录不重命名
                file_name_re = (
                    share_file["file_name"]
                    if share_file["dir"]
                    else mr.sub(pattern, replace, share_file["file_name"])
                )
                if file_name_saved := mr.is_exists(
                    file_name_re,
                    dir_filename_list,
                    (task.get("ignore_extension") and not share_file["dir"]),
                ):
                    share_file["file_name_saved"] = file_name_saved
                else:
                    share_file["file_name_re"] = file_name_re

        # 文件列表排序
        if re.search(r"\{I+\}", replace):
            mr.set_dir_file_list(dir_file_list, replace)
            mr.sort_file_list(data["list"])

    if request.json.get("task"):
        preview_regex(data)

    return jsonify({"success": True, "data": data})


@app.route("/get_savepath_detail")
def get_savepath_detail():
    if not is_login():
        return jsonify({"success": False, "message": "未登录"})
    try:
        if fid := request.args.get("fid", None):
            file_list = _get_file_list(fid=fid)
        elif path := request.args.get("path", "/"):
            file_list = _get_file_list(path=path)
        return jsonify({"success": True, "data": file_list})
    except Exception as e:
        return jsonify({"success": False, "data": {"error": str(e)}})


def _get_file_list(fid: str = None, path: str = None):
    account = Quark(config_data["cookie"][0])
    paths = []
    if path and not fid:
        path = re.sub(r"/+", "/", path)
        if path == "/":
            fid = 0
        else:
            dir_names = path.split("/")
            if dir_names[0] == "":
                dir_names.pop(0)
            path_fids = []
            current_path = ""
            for dir_name in dir_names:
                current_path += "/" + dir_name
                path_fids.append(current_path)
            if get_fids := account.get_fids(path_fids):
                fid = get_fids[-1]["fid"]
                paths = [
                    {"fid": get_fid["fid"], "name": dir_name}
                    for get_fid, dir_name in zip(get_fids, dir_names)
                ]
            else:
                raise FileNotFoundError("获取fid失败")
    file_list = {
        "fid": fid,
        "list": account.ls_dir(fid)["data"]["list"],
        "paths": paths,
    }
    return file_list


def _path_to_fid(path):
    """根据路径获取文件的fid"""
    if not path:
        raise ValueError("路径不能为空")
    path = re.sub(r"/+", "/", path)
    if path == "/":
        return 0
    file_list = _get_file_list(None, os.path.dirname(path))
    for file in file_list["list"]:
        if file["file_name"] == os.path.basename(path):
            return file["fid"]
    raise FileNotFoundError(f"未找到文件: {path}")


@app.route("/delete_file", methods=["POST"])
def delete_file():
    if not is_login():
        return jsonify({"success": False, "message": "未登录"})
    try:
        fid = request.json.get("fid") or _path_to_fid(request.json.get("path"))
        if fid:
            account = Quark(config_data["cookie"][0])
            response = account.delete([fid])
            response["success"] = response["code"] == 0
            return jsonify(response)
        else:
            raise ValueError("缺失必要字段: fid 或 path")
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})


@app.route("/rename_file", methods=["POST"])
def rename_file():
    if not is_login():
        return jsonify({"success": False, "message": "未登录"})
    try:
        fid = request.json.get("fid") or _path_to_fid(request.json.get("path"))
        file_name = request.json.get("file_name")
        if fid and file_name:
            account = Quark(config_data["cookie"][0])
            response = account.rename(fid, file_name)
            response["success"] = response["code"] == 0
            return jsonify(response)
        else:
            raise ValueError("缺失必要字段: fid, file_name")
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})


# 添加任务接口
@app.route("/api/add_task", methods=["POST"])
def add_task():
    global config_data
    # 验证token
    if not is_login():
        return jsonify({"success": False, "code": 1, "message": "未登录"}), 401
    # 必选字段
    request_data = request.json
    required_fields = ["taskname", "shareurl", "savepath"]
    for field in required_fields:
        if field not in request_data or not request_data[field]:
            return (
                jsonify(
                    {"success": False, "code": 2, "message": f"缺少必要字段: {field}"}
                ),
                400,
            )
    if not request_data.get("addition"):
        request_data["addition"] = task_plugins_config_default
    # 先从磁盘加载最新任务列表，避免内存中的过期数据覆盖已有任务
    reload_config_data()
    config_data.setdefault("tasklist", []).append(request_data)
    Config.write_json(CONFIG_PATH, config_data)
    logging.info(f">>> 通过API添加任务: {request_data['taskname']}")
    return jsonify(
        {"success": True, "code": 0, "message": "任务添加成功", "data": request_data}
    )


# 定时任务执行的函数
def run_python(args):
    logging.info(f">>> 定时运行任务")
    try:
        result = subprocess.run(
            f"{PYTHON_PATH} {args}",
            shell=True,
            timeout=TASK_TIMEOUT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        # 输出执行日志
        if result.stdout:
            for line in result.stdout.strip().split("\n"):
                if line.strip():
                    logging.info(line)

        if result.returncode == 0:
            logging.info(f">>> 任务执行成功")
        else:
            logging.error(f">>> 任务执行失败，返回码: {result.returncode}")
            if result.stderr:
                logging.error(f"错误信息: {result.stderr[:500]}")
    except subprocess.TimeoutExpired as e:
        logging.error(f">>> 任务执行超时(>{TASK_TIMEOUT}s)，强制终止")
    except Exception as e:
        logging.error(f">>> 任务执行异常: {str(e)}")
        logging.error(traceback.format_exc())
    finally:
        # 确保函数能够正常返回
        logging.debug(f">>> run_python 函数执行完成")


# 重新加载任务
def reload_tasks():
    # 读取定时规则
    if crontab := config_data.get("crontab"):
        if scheduler.state == 1:
            scheduler.pause()  # 暂停调度器
        trigger = CronTrigger.from_crontab(crontab)
        scheduler.remove_all_jobs()
        scheduler.add_job(
            run_python,
            trigger=trigger,
            args=[f"{SCRIPT_PATH} {CONFIG_PATH}"],
            id=SCRIPT_PATH,
            max_instances=1,  # 最多允许1个实例运行
            coalesce=True,  # 合并错过的任务，避免堆积
            misfire_grace_time=300,  # 错过任务的宽限期(秒)，超过则跳过
            replace_existing=True,  # 替换已存在的同ID任务
        )
        if scheduler.state == 0:
            scheduler.start()
        elif scheduler.state == 2:
            scheduler.resume()
        scheduler_state_map = {0: "停止", 1: "运行", 2: "暂停"}
        logging.info(">>> 重载调度器")
        logging.info(f"调度状态: {scheduler_state_map[scheduler.state]}")
        logging.info(f"定时规则: {crontab}")
        logging.info(f"现有任务: {scheduler.get_jobs()}")
        return True
    else:
        logging.info(">>> no crontab")
        return False


def init():
    global config_data, task_plugins_config_default
    logging.info(">>> 初始化配置")
    # 检查配置文件是否存在
    if not os.path.exists(CONFIG_PATH):
        if not os.path.exists(os.path.dirname(CONFIG_PATH)):
            os.makedirs(os.path.dirname(CONFIG_PATH))
        with open("quark_config.json", "rb") as src, open(CONFIG_PATH, "wb") as dest:
            dest.write(src.read())

    # 读取配置
    config_data = Config.read_json(CONFIG_PATH)
    Config.breaking_change_update(config_data)
    if not config_data.get("magic_regex"):
        config_data["magic_regex"] = MagicRename().magic_regex

    # 默认管理账号
    config_data["webui"] = {
        "username": os.environ.get("WEBUI_USERNAME")
        or config_data.get("webui", {}).get("username", "admin"),
        "password": os.environ.get("WEBUI_PASSWORD")
        or config_data.get("webui", {}).get("password", "admin123"),
    }

    # 默认定时规则
    if not config_data.get("crontab"):
        config_data["crontab"] = "0 8,18,20 * * *"

    # 初始化插件配置
    _, plugins_config_default, task_plugins_config_default = Config.load_plugins()
    for name, config in plugins_config_default.items():
        for key, value in config.items():
            config[key] = (
                config_data.setdefault("plugins", {})
                .setdefault(name, {})
                .get(key, value)
            )
    config_data["plugins"] = plugins_config_default

    # 更新配置
    Config.write_json(CONFIG_PATH, config_data)


if __name__ == "__main__":
    init()
    reload_tasks()
    logging.info(">>> 启动Web服务")
    logging.info(f"运行在: http://{HOST}:{PORT}")
    app.run(
        debug=DEBUG,
        host=HOST,
        port=PORT,
    )
