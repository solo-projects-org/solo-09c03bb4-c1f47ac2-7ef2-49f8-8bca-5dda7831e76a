"""故障注入：串页 + 令牌失效合计重抓一次后仍失败，用于验证整批回滚。

单独放在 scripts/，不进入应用镜像。
"""
import os
import sys

sys.path.insert(0, "/workspace")

from app.oai_client import OAIClient, OAIError, OAIPage  # noqa: E402


class FlakyClient(OAIClient):
    """每次续页都失败：第一次串页（pageMismatch），重抓时令牌失效。"""

    def __init__(self):  # 不建立真实 HTTP 连接
        self.resume_calls = 0

    def list_records_first(self, *, from_date, until_date):
        return OAIPage(
            records=[{"fake": True}],  # 占位，模拟首页已取到 10 条
            resumption_token="token-a",
            complete_list_size=27,
            cursor=0,
        )

    def list_records_resume(self, token):
        self.resume_calls += 1
        if self.resume_calls == 1:
            raise OAIError("pageMismatch", "模拟串页：响应属于别的请求")
        raise OAIError("badResumptionToken", "模拟重抓时令牌再次失效")


from app import db, harvester  # noqa: E402

db_path = os.environ.get("APP_DB_PATH", "/tmp/e2e/data/failtest.db")
os.makedirs(os.path.dirname(db_path), exist_ok=True)
for suffix in ("", "-wal", "-shm"):
    try:
        os.remove(db_path + suffix)
    except FileNotFoundError:
        pass
db.DB_PATH = db_path  # harvest 内部用 get_connection() 取同一路径
db.init_db()
before = db.get_last_until(db.get_connection())
print("采集前 last_until:", before)

try:
    harvester.harvest(
        FlakyClient(), until_date="2026-10-27", from_date="2026-10-01"
    )
except OAIError as e:
    print("预期失败抛出:", e)

conn = db.get_connection()
print("采集后 last_until（应仍为 None）:", db.get_last_until(conn))
print("书目条数（应为 0，半批未入库）:",
      conn.execute("SELECT COUNT(*) FROM bib_records").fetchone()[0])
run = conn.execute(
    "SELECT status, retries, error FROM harvest_runs ORDER BY id DESC LIMIT 1"
).fetchone()
print("最近运行:", dict(run))
conn.close()
