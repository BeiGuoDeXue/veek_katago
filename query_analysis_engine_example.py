"""
This is a simple python program that demonstrates how to run KataGo's
analysis engine as a subprocess and send it a query. It queries the
result of playing the 4-4 point on an empty board and prints out
the json response.

输出参考：
1、rootInfo 根节点信息，这是对整个局面的全局评估：
"rootInfo": {
  "scoreLead": 5.32,     // 当前行棋方预期领先分数（单位：目）
  "scoreSelfplay": 5.41, // 自对弈模拟中的预期得分
  "winrate": 0.673,      // 当前行棋方胜率（1.0=100%）
  "visits": 1500         // 总搜索节点数
}
解读重点​​：

scoreLead：正数表示​​当前行棋方​​领先（如黑方行棋时+5.32表示黑领先5.32目）
winrate：直接反映当前局面优劣（0.673=67.3%胜率）
2、moveInfos 候选着法分析​， 对每个可能着法的详细评估列表：
"moveInfos": [
  {
    "move": "Q16",        // 推荐着法坐标（如Q16）
    "visits": 850,        // 该着法的搜索深度（越高表示引擎越重视此着法）
    "winrate": 0.682,     // 该着法后的胜率变化（对比rootInfo.winrate判断是否改善局面）
    "scoreLead": 6.15,    // 该着法后的分数变化
    "prior": 0.215,       // 神经网络原始评估概率（未搜索前）
    "order": 0,           // 推荐排名（0=最佳）
    "pv": ["Q16","P17",...] // ​​后续变化图​​（Principal Variation），展示双方最佳应对的参考棋谱
  },
  {
    "move": "R17",
    "visits": 320,
    "winrate": 0.621,
    ...
  }
]

3. ownership 归属图 ​可视化输出每个交叉点的控制权预测：
"ownership": [
  [ 0.8, 0.7, -0.3, ... ],  // 第1行
  [-0.9, 0.1, 0.4, ... ],   // 第2行
  ...                       // 19x19数组
]
数值解读​​：

​​正值​​：黑棋控制概率（1.0=完全控制）
​​负值​​：白棋控制概率（-1.0=完全控制）
​​接近0​​：无明确归属（如中腹未定形区域）
注：需配合GUI工具可视化（如Sabaki的KataGo插件）

4. policy 策略热力图​ 神经网络对各着法的原始评估（非搜索结果）
"policy": [
  [0.01, 0.003, ...],  // 第1行各点概率
  [0.05, 0.12, ...]    // 第2行各点概率
]
特点​​：

数值范围：0.0~1.0
反映​​直觉判断​​（非深度计算）
与moveInfos.prior数据同源


​​5. 高级参数（专业分析）​​
参数	    意义
lcb	        置信下限（Low Confidence Bound），评估着法稳定性
utility	    综合效用值（结合胜率、目差等因素的内部评估）
scoreStdev	目差预测的标准差（值越大表示局势越不确定）

使用技巧​​
1、关注变化量​​：比绝对值更重要的是着法带来的winrate/scoreLead变化
2、结合PV序列​​：通过pv字段预演后续10-15步关键变化
​​3、对比分析​​：
    改善度 = moveInfos.winrate - rootInfo.winrate
    正改善度越大，着法提升效果越显著
4、稳定性判断​​：高visits+低scoreStdev=可靠着法
"""

import argparse
import json
import os
import subprocess
import sys
import time
from threading import Thread
import sgfmill
import sgfmill.boards
import sgfmill.ascii_boards
from serial import Serial
from typing import Tuple, List, Optional, Union, Literal, Any, Dict
import logging
from pathlib import Path
import signal
import atexit

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('go_analysis.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# 全局变量用于清理资源
katago_process = None
serial_port = None

def cleanup_resources():
    """清理所有资源"""
    global katago_process, serial_port
    try:
        if katago_process:
            logger.info("正在关闭KataGo进程...")
            katago_process.terminate()
            katago_process.wait(timeout=5)
        if serial_port and serial_port.is_open:
            logger.info("正在关闭串口...")
            serial_port.close()
    except Exception as e:
        logger.error(f"清理资源时发生错误: {e}")

# 注册清理函数
atexit.register(cleanup_resources)

def signal_handler(signum, frame):
    """处理系统信号"""
    logger.info(f"收到信号 {signum}，正在清理资源...")
    cleanup_resources()
    sys.exit(0)

# 注册信号处理器
signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

Color = Union[Literal["b"],Literal["w"]]
Move = Union[None,Literal["pass"],Tuple[int,int]]

def validate_path(path: str, description: str) -> str:
    """验证路径是否存在"""
    if not os.path.exists(path):
        raise FileNotFoundError(f"{description}不存在: {path}")
    return path

def sgfmill_to_str(move: Move) -> str:
    """围棋坐标转换"""
    if move is None:
        return "pass"
    if move == "pass":
        return "pass"
    try:
        (y,x) = move
        if not (0 <= x < 19 and 0 <= y < 19):
            raise ValueError(f"无效的坐标: ({x}, {y})")
        return "ABCDEFGHJKLMNOPQRSTUVWXYZ"[x] + str(y+1)
    except Exception as e:
        logger.error(f"坐标转换错误: {e}")
        raise

def convert_to_robot_coordinates(board_x: int, board_y: int) -> Tuple[float, float]:
    """
    将棋盘坐标转换为机械臂坐标
    棋盘坐标: (x, y) 范围 0-18
    机械臂坐标: (a, b)
    转换公式:
    a = (289/12)x + (11/16)y + 1807/24
    b = -(5/4)x + (47/2)y - 220
    """
    # 将棋盘坐标加1
    board_x += 1
    board_y += 1
    
    a = (289/12) * board_x + (11/16) * board_y + 1807/24
    b = -(5/4) * board_x + (47/2) * board_y - 220
    print(f"棋盘坐标(x,y): ({board_x}, {board_y}) -> 机械臂坐标(a,b): ({a:.2f}, {b:.2f})")
    return a, b

def convert_to_mp_format(move: str) -> str:
    """将围棋坐标转换为机械臂能识别的格式"""
    try:
        if move == "pass":
            return "MP X 0 Y 0 Z 0 A 0 S 100#\r\n"
        
        # 将字母转换为数字 (A=0, B=1, ..., Z=25)
        x = ord(move[0]) - ord('A')
        if x > 8:  # 跳过I
            x -= 1
        y = int(move[1:]) - 1
        
        if not (0 <= x < 19 and 0 <= y < 19):
            raise ValueError(f"无效的坐标: {move}")
            
        print(f"围棋坐标 {move} -> 棋盘坐标(x,y): ({x}, {y})")
        
        # 转换为机械臂坐标
        a, b = convert_to_robot_coordinates(x, y)
        mp_command = f"MP X {a:.2f} Y {b:.2f} Z 0 A 0 S 100#\r\n"
        print(f"机械臂命令: {mp_command.strip()}")
        return mp_command
    except Exception as e:
        logger.error(f"MP格式转换错误: {e}")
        raise

def format_analysis_result(result: Dict[str, Any]) -> str:
    """格式化分析结果为人类可读的格式"""
    try:
        output = []
        
        # 基本信息
        root_info = result.get('rootInfo', {})
        output.append("=== 当前局面分析 ===")
        output.append(f"当前玩家: {'白方' if root_info.get('currentPlayer') == 'W' else '黑方'}")
        output.append(f"总节点搜索深度: {root_info.get('visits', 0)}")
        output.append(f"当前胜率: {root_info.get('winrate', 0)*100:.2f}%")
        output.append(f"领先分数: {root_info.get('scoreLead', 0):.2f}目")
        output.append(f"自对弈得分: {root_info.get('scoreSelfplay', 0):.2f}目")
        output.append(f"得分标准差: {root_info.get('scoreStdev', 0):.2f}目")
        output.append("")

        # 最佳着法分析
        output.append("=== 最佳着法分析 ===")
        
        move_infos = result.get('moveInfos', [])
        if move_infos:
            sorted_moves = sorted(move_infos, key=lambda x: x.get('winrate', 0), reverse=True)
            
            for i, move_info in enumerate(sorted_moves[:5], 1):
                move = move_info.get('move', '')
                winrate = move_info.get('winrate', 0) * 100
                visits = move_info.get('visits', 0)
                prior = move_info.get('prior', 0)
                lcb = move_info.get('lcb', 0) * 100
                utility = move_info.get('utility', 0)
                score_mean = move_info.get('scoreMean', 0)
                score_stdev = move_info.get('scoreStdev', 0)
                pv = move_info.get('pv', [])
                
                output.append(f"\n{i}. {move}点:")
                output.append(f"   走这一步后的胜率: {winrate:.2f}%")
                output.append(f"   节点搜索深度: {visits}")
                output.append(f"   先验概率: {prior:.4f}")
                output.append(f"   置信区间下界: {lcb:.2f}%")
                output.append(f"   效用值: {utility:.4f}")
                output.append(f"   得分期望: {score_mean:.2f}目")
                output.append(f"   得分标准差: {score_stdev:.2f}目")
                output.append(f"   MP格式: {convert_to_mp_format(move)}")
                
                if pv:
                    output.append("   后续变化:")
                    for j, pv_move in enumerate(pv, 1):
                        output.append(f"     {j}. {pv_move}")
        else:
            output.append("未找到着法信息")
        
        return "\n".join(output)
    except Exception as e:
        logger.error(f"格式化分析结果时发生错误: {e}")
        raise

def init_serial(serial_port: str = 'COM6', baud_rate: int = 9600) -> Optional[Serial]:
    """初始化串口"""
    try:
        uart = Serial(
            port=serial_port,
            baudrate=baud_rate,
            bytesize=8,
            parity='N',
            stopbits=1,
            timeout=1
        )
        if not uart.is_open:
            uart.open()
        time.sleep(1)  # 等待串口稳定
        logger.info(f"串口 {serial_port} 初始化成功")
        return uart
    except Exception as e:
        logger.error(f"串口初始化错误: {e}")
        return None

def send_init_commands(uart: Serial) -> bool:
    """发送初始化指令"""
    uart.write(b"HOME#\r\n")
    time.sleep(10)
    response = uart.readline()
    if response:
        logger.info(f"原点定位指令返回值: {response.decode('utf-8', 'ignore').strip()}")
    uart.write(b"HOME#\r\n")
    time.sleep(10)
    response = uart.readline()
    if response:
        logger.info(f"指令返回值: {response.decode('utf-8', 'ignore').strip()}")
    uart.write(b"MP X 97 Y -200 Z 0 A 0 S 100#\r\n")
    time.sleep(10)
    response = uart.readline()
    if response:
        logger.info(f"拿棋子指令返回值: {response.decode('utf-8', 'ignore').strip()}")
    # try:
    #     commands = [
    #         ('HOME#\r\n', "HOME"),
    #         ('HOME#\r\n', "HOME"),
    #         ('MP X 97 Y -196 Z 0 A 0 S 100#\r\n', "检查A1坐标是否准确")
    #     ]
        
    #     for cmd, desc in commands:
    #         logger.info(f"发送 {desc} 指令...")
    #         uart.write(cmd.encode())
    #         time.sleep(5)
    #         response = uart.read()
    #         if response:
    #             logger.info(f"{desc} 指令返回值: {response.decode('utf-8', 'ignore')}")
    #         else:
    #             logger.warning(f"{desc} 指令未收到响应")
    #             return False
    #     return True
    # except Exception as e:
    #     logger.error(f"发送初始化指令错误: {e}")
    #     return False

def send_move_to_serial(move: str, uart: Serial) -> bool:
    """发送着法坐标到串口"""
    try:
        # 发送指令前，先清空串口的输入缓冲区，防止读取到旧的残留数据
        uart.reset_input_buffer()
        uart.write(b"MP X 97 Y -200 Z 0 A 0 S 100#\r\n")
        time.sleep(10)
        response = uart.readline()
        if response:
           logger.info(f"指令返回值: {response.decode('utf-8', 'ignore').strip()}")
        
        uart.write(b"Set 1 #\r\n")
        time.sleep(10)
        response = uart.readline()
        if response:
           logger.info(f"Set 1指令返回值: {response.decode('utf-8', 'ignore').strip()}")

        uart.write(b"MP X 97 Y -200 Z 32.5 A 0 S 100#\r\n")
        time.sleep(15)
        response = uart.readline()
        if response:
           logger.info(f"指令返回值: {response.decode('utf-8', 'ignore').strip()}")     
        mp_command = convert_to_mp_format(move)
        logger.info(f"发送着法 {move} ({mp_command.strip()}) 到串口...")

        uart.write(mp_command.encode())
        time.sleep(5)
        response = uart.readline()
        if response:
            logger.info(f"指令返回值: {response.decode('utf-8', 'ignore').strip()}")
            # 检查响应中是否包含"ok"，如果有就认为执行成功
            response_str = response.decode('utf-8', 'ignore').strip()
            if "ok" in response_str.lower():
                logger.info("移动指令执行成功")
            else:
                logger.warning("移动指令响应中未包含'ok'，可能执行失败")
        else:
            logger.warning("未收到响应")
            return False
        
        uart.write(b"Reset 1 #\r\n")
        time.sleep(5)
        response = uart.readline()
        if response:
           logger.info(f"指令返回值: {response.decode('utf-8', 'ignore').strip()}")     
        mp_command = convert_to_mp_format(move)
        logger.info(f"发送着法 {move} ({mp_command.strip()}) 到串口...")    
        # 移动完成后，发送HOME指令让机械臂回到原点
        logger.info("发送HOME指令，让机械臂回到原点...")
        time.sleep(5)
        logger.info("发送指令: HOME#")
        uart.write(b"HOME#\r\n")
        time.sleep(10)
        response = uart.readline()
        if response:
            logger.info(f"HOME指令返回值: {response.decode('utf-8', 'ignore').strip()}")
            # 检查HOME指令响应中是否包含"ok"
            response_str = response.decode('utf-8', 'ignore').strip()
            if "ok" in response_str.lower():
                logger.info("HOME指令执行成功")
                return True
            else:
                logger.warning("HOME指令响应中未包含'ok'，可能执行失败")
                return True  # 即使HOME失败也返回True，因为主要的移动指令已经执行了
        else:
            logger.warning("HOME指令未收到响应")
            return False

    except Exception as e:
        logger.error(f"发送着法错误: {e}")
        return False
    
class KataGo:
    def __init__(self, katago_path: str, config_path: str, model_path: str, additional_args: List[str] = []):
        """初始化KataGo引擎"""
        try:
            self.query_counter = 0
            global katago_process
            katago_process = subprocess.Popen(
                [katago_path, "analysis", "-config", config_path, "-model", model_path, *additional_args],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.katago = katago_process
            
            def printforever():
                while katago_process.poll() is None:
                    data = katago_process.stderr.readline()
                    time.sleep(0)
                    if data:
                        logger.info(f"KataGo: {data.decode()}")
                data = katago_process.stderr.read()
                if data:
                    logger.info(f"KataGo: {data.decode()}")
                    
            self.stderrthread = Thread(target=printforever)
            self.stderrthread.daemon = True
            self.stderrthread.start()
            
            # 等待引擎初始化
            time.sleep(2)
            if katago_process.poll() is not None:
                raise RuntimeError("KataGo进程意外退出")
                
        except Exception as e:
            logger.error(f"初始化KataGo引擎失败: {e}")
            raise

    def close(self):
        """关闭KataGo引擎"""
        try:
            if hasattr(self, 'katago') and self.katago:
                self.katago.stdin.close()
        except Exception as e:
            logger.error(f"关闭KataGo引擎时发生错误: {e}")

    def query(self, initial_board: sgfmill.boards.Board, moves: List[Tuple[Color,Move]], komi: float, max_visits=None):
        """发送查询到KataGo引擎"""
        try:
            query = {
                "id": str(self.query_counter),
                "moves": [(color,sgfmill_to_str(move)) for color, move in moves],
                "initialStones": [],
                "rules": "Chinese",
                "komi": komi,
                "boardXSize": initial_board.side,
                "boardYSize": initial_board.side,
                "includePolicy": True
            }
            
            self.query_counter += 1
            
            for y in range(initial_board.side):
                for x in range(initial_board.side):
                    color = initial_board.get(y,x)
                    if color:
                        query["initialStones"].append((color,sgfmill_to_str((y,x))))
                        
            if max_visits is not None:
                query["maxVisits"] = max_visits
                
            return self.query_raw(query)
        except Exception as e:
            logger.error(f"发送查询时发生错误: {e}")
            raise

    def query_raw(self, query: Dict[str,Any]):
        """发送原始查询到KataGo引擎"""
        try:
            self.katago.stdin.write((json.dumps(query) + "\n").encode())
            self.katago.stdin.flush()

            line = ""
            timeout = 30  # 设置30秒超时
            start_time = time.time()
            
            while line == "":
                if time.time() - start_time > timeout:
                    raise TimeoutError("KataGo响应超时")
                    
                if self.katago.poll():
                    raise RuntimeError("KataGo进程意外退出")
                    
                line = self.katago.stdout.readline()
                line = line.decode().strip()
                
            response = json.loads(line)
            return response
        except Exception as e:
            logger.error(f"发送原始查询时发生错误: {e}")
            raise

def main():
    """主函数"""
    try:
        parser = argparse.ArgumentParser(description="围棋分析引擎示例程序")
        parser.add_argument(
            "-katago-path",
            help="KataGo可执行文件路径",
            default="./katago-v1.16.2-eigenavx2-windows-x64/katago.exe",
        )
        parser.add_argument(
            "-config-path",
            help="KataGo配置文件路径",
            default="./katago-v1.16.2-eigenavx2-windows-x64/analysis_example.cfg",
        )
        parser.add_argument(
            "-model-path",
            help="神经网络模型文件路径",
            default="./katago-v1.16.2-eigenavx2-windows-x64/g170e-b20c256x2.bin.gz",
        )
        parser.add_argument(
            "-output-json",
            help="分析结果JSON文件保存路径",
            default="analysis_result.json",
        )
        parser.add_argument(
            "-serial-port",
            help="串口名称 (例如: COM1)",
            default="COM6",
        )
        parser.add_argument(
            "-baud-rate",
            help="波特率",
            type=int,
            default=9600,
        )
        args = vars(parser.parse_args())
        
        # 验证文件路径
        katago_path = validate_path(args["katago_path"], "KataGo可执行文件")
        config_path = validate_path(args["config_path"], "KataGo配置文件")
        model_path = validate_path(args["model_path"], "神经网络模型文件")
        
        # 初始化串口
        global serial_port
        serial_port = init_serial(args["serial_port"], args["baud_rate"])
        if serial_port is None:
            logger.error("串口初始化失败，程序退出")
            sys.exit(1)

        # 发送初始化指令
        send_init_commands(serial_port)
        #     logger.error("初始化指令发送失败，程序退出")
        #     sys.exit(1)

        # 初始化KataGo
        katago = KataGo(katago_path, config_path, model_path)

        # 设置棋盘
        board = sgfmill.boards.Board(19)
        komi = 6.5
        moves = [("b",(3,3)),("w",(3,4)),("b",(3,5))]

        # 显示当前棋盘状态
        displayboard = board.copy()
        for color, move in moves:
            if move != "pass":
                row,col = move
                displayboard.play(row,col,color)
        logger.info("\n=== 当前棋盘状态 ===")
        logger.info(sgfmill.ascii_boards.render_board(displayboard))

        # 获取分析结果
        logger.info("\n=== 分析结果 ===")
        result = katago.query(board, moves, komi)
        
        # 保存原始分析结果
        output_json = args["output_json"]
        with open(output_json, 'w', encoding='utf-8') as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        logger.info(f"\n原始分析结果已保存到: {output_json}")
        
        # 显示格式化的分析结果
        formatted_result = format_analysis_result(result)
        logger.info(formatted_result)

        # 获取最佳着法并发送到串口
        move_infos = result.get('moveInfos', [])
        if move_infos:
            sorted_moves = sorted(move_infos, key=lambda x: x.get('winrate', 0), reverse=True)
            best_move = sorted_moves[0].get('move', '')
            if best_move:
                logger.info(f"\n发送最佳着法坐标 {best_move} 到串口...")
                if not send_move_to_serial(best_move, serial_port):
                    logger.error("发送着法失败")
        else:
            logger.warning("未找到最佳着法")

    except Exception as e:
        logger.error(f"程序执行过程中发生错误: {e}")
        sys.exit(1)
    finally:
        cleanup_resources()

if __name__ == "__main__":
    main()

