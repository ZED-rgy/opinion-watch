"""让 `python -m opinion_watch.desktop` 可以启动桌面应用。

开机自启注册表命令依赖这个入口；没有它时该命令只会导入模块然后
静默退出。
"""

from opinion_watch.desktop import main

if __name__ == "__main__":
    main()
