"""wb2api 的单元测试。

全部用标准库 unittest，不引入 pytest：这个项目的部署场景是一台只有 Python 的机器，
"pip install 一堆东西才能跑测试" 会让它在最需要验证的地方最不可验证。

    python -m unittest discover -s tests -t .
"""
