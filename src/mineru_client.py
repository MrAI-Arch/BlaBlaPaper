"""
Mineru PDF 解析服务客户端模块
"""
import requests
import time
import os
from . import config
from . import logutil


class MineruClient:
    """Mineru API 客户端类"""

    def __init__(self, api_token=None):
        """
        初始化客户端

        Args:
            api_token: API Token，默认使用 config.MINERU_API_TOKEN
        """
        self.api_token = api_token or config.MINERU_API_TOKEN
        self.base_url = config.MINERU_BASE_URL
        self.headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_token}"
        }

    def upload_file(self, file_path):
        """
        上传 PDF 文件到 Mineru 服务

        Args:
            file_path: 本地 PDF 文件路径

        Returns:
            batch_id (str): 上传成功返回批次 ID，失败返回 None
        """
        if not os.path.exists(file_path):
            logutil.log(f"[Error] 文件不存在: {file_path}", "ERROR")
            return None

        file_name = os.path.basename(file_path)
        url = f"{self.base_url}/file-urls/batch"

        # 构造请求体
        payload = {
            "files": [
                {"name": file_name, "data_id": f"task_{int(time.time())}"}
            ],
            "model_version": config.MINERU_MODEL_VERSION
        }

        try:
            logutil.log(f"[1/4] 正在申请上传链接: {file_name} ...", "INFO")
            response = requests.post(url, headers=self.headers, json=payload)
            result = response.json()

            if result.get("code") != 0:
                logutil.log(f"[Error] 申请失败: {result.get('msg')}", "ERROR")
                return None

            batch_id = result["data"]["batch_id"]
            upload_url = result["data"]["file_urls"][0]

            # 上传文件 (PUT 请求)
            logutil.log(f"[2/4] 正在上传文件内容...", "INFO")
            with open(file_path, 'rb') as f:
                # 注意：上传文件不需要 Content-Type application/json，直接传二进制
                upload_headers = {}
                res_upload = requests.put(upload_url, data=f, headers=upload_headers)

                if res_upload.status_code == 200:
                    logutil.log(f"      上传成功! Batch ID: {batch_id}", "INFO")
                    return batch_id
                else:
                    logutil.log(f"[Error] 文件上传失败, 状态码: {res_upload.status_code}", "ERROR")
                    return None

        except Exception as e:
            logutil.log(f"[Exception] 上传过程发生异常: {e}", "ERROR")
            return None

    def poll_status(self, batch_id):
        """
        轮询任务状态直到完成

        Args:
            batch_id: 任务批次 ID

        Returns:
            download_url (str): 完成时返回下载链接，失败返回 None
        """
        url = f"{self.base_url}/extract-results/batch/{batch_id}"
        logutil.log(f"[3/4] 开始等待解析完成 (每5秒查询一次)...", "INFO")

        while True:
            try:
                response = requests.get(url, headers=self.headers)
                res_json = response.json()

                if res_json.get("code") != 0:
                    logutil.log(f"[Error] 查询状态失败: {res_json.get('msg')}", "ERROR")
                    return None

                # 获取第一个文件的结果（因为我们只传了一个）
                file_result = res_json["data"]["extract_result"][0]
                state = file_result["state"]

                if state == "done":
                    logutil.log(f"\n      解析完成！", "INFO")
                    return file_result["full_zip_url"]

                elif state == "failed":
                    error_msg = file_result.get("err_msg", "未知错误")
                    logutil.log(f"\n[Error] 解析失败: {error_msg}", "ERROR")
                    return None

                elif state == "running":
                    progress = file_result.get("extract_progress", {})
                    curr = progress.get("extracted_pages", 0)
                    total = progress.get("total_pages", "?")
                    logutil.progress(f"\r      正在解析中... 已处理: {curr}/{total} 页")

                else:
                    # 其他状态: pending, waiting-file, converting
                    logutil.progress(f"\r      当前状态: {state} ...")

                # 等待5秒再次查询
                time.sleep(5)

            except Exception as e:
                logutil.log(f"\n[Exception] 轮询过程发生异常: {e}", "ERROR")
                time.sleep(5)

    def download_file(self, url, save_path):
        """
        下载解析结果 ZIP 文件

        Args:
            url: 下载链接
            save_path: 本地保存路径

        Returns:
            bool: 下载成功返回 True，失败返回 False
        """
        logutil.log(f"[4/4] 正在下载结果文件...", "INFO")
        try:
            with requests.get(url, stream=True) as r:
                r.raise_for_status()
                with open(save_path, 'wb') as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        f.write(chunk)
            logutil.log(f"[Success] 文件已保存至: {os.path.abspath(save_path)}", "INFO")
            return True
        except Exception as e:
            logutil.log(f"[Error] 下载文件失败: {e}", "ERROR")
            return False
