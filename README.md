# 维克未来的围棋机器人集成
主程序：
    query_analysis_engine_example.py

运行依赖：
    需要安装requirements.txt的依赖
    需要把引擎g170e-b20c256x2.bin.gz放到katago-v1.16.2-eigenavx2-windows-x64下

运行命令：
```
python query_analysis_engine_example.py --use-vision --template-file .\go_board_recognition\template_6_10_new_jpg.gpar --serial-port COM13 --baud-rate 9600 --board-size 9 --camera-index 1 --loop-mode --wait-time 8
```

目前开发进度:
- [x] 集成了开源的katago
- [x] 运行的是cpu版本的katago，可以正常运行
- [x] 集成了机械臂的控制
- [x] 待加入视觉识别棋盘
- [x] 待整体调通
- [ ] 待更换gpu版本的katago
- [x] 待开发主程序逻辑
- [ ] 配置难度
- [ ] 设置黑棋或者白棋
- [ ] 拣棋盘上的棋子
- [ ] 判断谁胜利

bug:
- [ ] 有的地方已经有棋子了，还往里面下


