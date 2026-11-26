import os
from app.factory import create_app

app = create_app()

if __name__ == "__main__":
    port = int(os.environ.get("端口", "8500"))
    # threaded=True：并发双请求脚本会两线程同时打同一事件，竞争同一把退避事件锁
    app.run(host="0.0.0.0", port=port, threaded=True)
