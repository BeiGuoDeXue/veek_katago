import subprocess
import time

# 启动 KataGo 进程
katago = subprocess.Popen(
    [".\katago.exe", "gtp", "-model", "g170e-b20c256x2.bin.gz", "-config", "gtp_amd5800H_16C16G.cfg"],
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True
)

# 发送命令
commands = [
    "boardsize 19", # 设置棋盘大小为19*19
    "clear_board", # 清空棋盘
    "komi 6.5", # 设置贴目为6.5目
    "kata-set-rules chinese", # 设置规则为中式围棋
    "set_position B Q16 W D4 B D16 W Q4 B K10 W C10", # 设置棋盘上的棋子位置
    "kata-analyze interval=1000 maxmoves=10 ownership=true" # 请求分析请求分析，每1000毫秒更新一次，最多显示10个变化，并计算领地归属
]

for cmd in commands:
    katago.stdin.write(cmd + "\n")
    katago.stdin.flush()
    
    # 读取响应直到出现两个连续的换行符
    response = ""
    while True:
        line = katago.stdout.readline()
        response += line
        if line.strip() == "":
            break
    
    print(f"Command: {cmd}")
    print(f"Response: {response}")
    
# 对于分析命令，需要等待一段时间后发送停止命令
time.sleep(5)  # 等待5秒
katago.stdin.write("stop\n")
katago.stdin.flush()

# 关闭进程
katago.terminate()