import requests
import openai
import re
import plugins
import os
import json
from plugins import *
from bridge.context import ContextType
from bridge.reply import Reply, ReplyType
from common.log import logger
from datetime import datetime, timedelta
from collections import defaultdict
import traceback

# OpenAI配置
OPENAI_API_KEY = None  # 从配置文件中加载
OPENAI_API_BASE = "https://api.openai.com/v1"  # 默认API基础URL
OPENAI_MODEL = "gpt-3.5-turbo"  # 默认模型
OPENAI_API_VERSION = "v1"  # 默认API版本
USE_OPENAI = False  # 是否使用OpenAI筛选功能

# 预定义热门中转站映射
TRANSFER_STATIONS = {
    # 格式：("出发城市", "目的城市"): ["中转站1", "中转站2", ...]
    ("成都", "上海"): ["武汉", "郑州", "南京"],
    ("北京", "广州"): ["郑州", "武汉", "长沙"],
    ("西安", "上海"): ["郑州", "合肥"],
    ("北京", "成都"): ["郑州", "西安"],
    ("广州", "北京"): ["武汉", "郑州"],
    ("上海", "成都"): ["武汉", "重庆"],
    ("深圳", "北京"): ["长沙", "武汉", "郑州"],
    ("重庆", "上海"): ["武汉", "合肥"],
    ("杭州", "成都"): ["武汉", "重庆"],
    ("成都", "杭州"): ["重庆", "武汉"]
}

# 全国主要铁路枢纽站（用于动态计算中转）
MAJOR_STATIONS = [
    "北京", "上海", "广州", "深圳", "杭州", "南京", "武汉", 
    "郑州", "西安", "成都", "重庆", "长沙", "合肥", "济南",
    "天津", "沈阳", "哈尔滨", "太原", "兰州", "南昌", "昆明",
    "福州", "厦门", "宁波", "青岛", "大连", "贵阳"
]

# 尝试从插件目录加载配置
try:
    plugin_dir = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(plugin_dir, "config.json")
    logger.info(f"尝试从 {config_path} 加载配置")
    
    if os.path.exists(config_path):
        logger.info(f"配置文件存在，尝试读取内容")
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                file_content = f.read()
                logger.info(f"读取到配置文件内容: {file_content[:100]}...")
                plugin_config = json.loads(file_content)
                
            logger.info(f"配置文件解析结果: {json.dumps(plugin_config, ensure_ascii=False)}")
            
            OPENAI_API_KEY = plugin_config.get("open_ai_api_key")
            if OPENAI_API_KEY:
                OPENAI_API_BASE = plugin_config.get("open_ai_api_base", OPENAI_API_BASE)
                OPENAI_MODEL = plugin_config.get("open_ai_model", OPENAI_MODEL)
                # 检查API版本配置
                OPENAI_API_VERSION = plugin_config.get("open_ai_api_version", OPENAI_API_VERSION)
                logger.info(f"检测到API版本配置: {OPENAI_API_VERSION}")
                USE_OPENAI = True
                logger.info(f"从插件配置加载OpenAI设置成功！API密钥: {OPENAI_API_KEY[:8]}..., API基础URL: {OPENAI_API_BASE}, 模型: {OPENAI_MODEL}, API版本: {OPENAI_API_VERSION}")
            else:
                logger.warning("插件配置中未找到有效的OpenAI API密钥")
        except Exception as read_error:
            logger.error(f"读取配置文件出错: {read_error}")
            logger.error(traceback.format_exc())
    else:
        logger.warning(f"插件配置文件不存在: {config_path}")
        
    # 如果插件配置不可用，尝试从全局配置加载
    if not USE_OPENAI:
        logger.info("插件配置不可用，尝试从全局配置加载")
        try:
            from config import conf
            config = conf()
            if hasattr(config, "get"):
                OPENAI_API_KEY = config.get("open_ai_api_key")
                if OPENAI_API_KEY:
                    OPENAI_API_BASE = config.get("open_ai_api_base", OPENAI_API_BASE)
                    OPENAI_MODEL = config.get("open_ai_model", OPENAI_MODEL)
                    USE_OPENAI = True
                    logger.info(f"从全局配置加载OpenAI设置成功！API基础URL: {OPENAI_API_BASE}, 模型: {OPENAI_MODEL}")
                else:
                    logger.warning("全局配置中未找到有效的OpenAI API密钥")
        except Exception as e:
            logger.warning(f"加载全局OpenAI配置失败: {e}")
            
except Exception as e:
    logger.error(f"加载配置时出错: {e}")
    logger.error(traceback.format_exc())

# 初始化OpenAI客户端
if USE_OPENAI:
    try:
        openai.api_key = OPENAI_API_KEY
        openai.api_base = OPENAI_API_BASE
        logger.info(f"OpenAI客户端已初始化 - 基础URL: {openai.api_base}, API密钥前8位: {OPENAI_API_KEY[:8]}...")
    except Exception as init_error:
        logger.error(f"初始化OpenAI客户端失败: {init_error}")
        logger.error(traceback.format_exc())
        USE_OPENAI = False

logger.info(f"OpenAI筛选功能状态: {'已启用' if USE_OPENAI else '未启用'}")

# 高铁API基础URL
BASE_URL_HIGHSPEEDTICKET = "https://api.pearktrue.cn/api/highspeedticket"

@plugins.register(name="TicketQuery",
                  desc="智能票务查询插件",
                  version="0.1",
                  author="sllt",
                  desire_priority=100)
class TicketQuery(Plugin):
    content = None
    ticket_info_list = []
    intermediate_ticket_info_list = []
    conversation_history = []
    last_interaction_time = None
    is_approximate_time = False
    approximate_time = None
    original_query = None
    
    # 新增字段，用于保存原始查询结果
    original_data = []  # 存储原始查询结果
    total_data = []     # 存储当前筛选结果
    current_page = 1

    def __init__(self):
        super().__init__()
        self.handlers[Event.ON_HANDLE_CONTEXT] = self.on_handle_context
        
        # 初始化分页相关属性
        self.current_page = 1
        self.page_size = 10  # 每页显示10条
        self.last_query_params = None  # 保存上次查询参数
        # 初始化近似时间属性
        self.is_approximate_time = False
        self.approximate_time = None
        self.original_query = None
        
        # 重新加载OpenAI配置，确保配置正确加载
        self._load_openai_config()
        
        logger.info(f"[{__class__.__name__}] 插件初始化完成")
        logger.info(f"OpenAI使用状态: {'已启用' if USE_OPENAI else '未启用'}")
        if USE_OPENAI:
            logger.info(f"OpenAI配置信息: API密钥前8位={OPENAI_API_KEY[:8]}..., 基础URL={OPENAI_API_BASE}, 模型={OPENAI_MODEL}")

    def _load_openai_config(self):
        """重新加载OpenAI配置"""
        global USE_OPENAI, OPENAI_API_KEY, OPENAI_API_BASE, OPENAI_MODEL, OPENAI_API_VERSION
        
        logger.info("====== 重新加载OpenAI配置 ======")
        try:
            # 获取插件目录路径
            plugin_dir = os.path.dirname(os.path.abspath(__file__))
            config_path = os.path.join(plugin_dir, "config.json")
            logger.info(f"尝试从 {config_path} 加载配置")
            
            if os.path.exists(config_path):
                logger.info(f"配置文件存在，开始读取")
                try:
                    with open(config_path, 'r', encoding='utf-8') as f:
                        file_content = f.read()
                        logger.info(f"读取到配置文件内容: {file_content}")
                        plugin_config = json.loads(file_content)
                    
                    logger.info(f"配置文件解析结果: {json.dumps(plugin_config, ensure_ascii=False)}")
                    
                    # 提取OpenAI配置
                    OPENAI_API_KEY = plugin_config.get("open_ai_api_key")
                    if OPENAI_API_KEY:
                        OPENAI_API_BASE = plugin_config.get("open_ai_api_base", OPENAI_API_BASE)
                        OPENAI_MODEL = plugin_config.get("open_ai_model", OPENAI_MODEL)
                        OPENAI_API_VERSION = plugin_config.get("open_ai_api_version", OPENAI_API_VERSION)
                        
                        # 检查API基础URL是否已包含API版本
                        if OPENAI_API_BASE.endswith(f"/{OPENAI_API_VERSION}"):
                            logger.info(f"API基础URL已包含版本信息: {OPENAI_API_BASE}")
                        else:
                            # 如果URL不是以/结尾，添加/
                            if not OPENAI_API_BASE.endswith("/"):
                                OPENAI_API_BASE += "/"
                            # 再添加版本号（但不重复添加）
                            if not OPENAI_API_BASE.endswith(f"{OPENAI_API_VERSION}/"):
                                OPENAI_API_BASE += f"{OPENAI_API_VERSION}"
                            logger.info(f"调整后的API基础URL: {OPENAI_API_BASE}")
                        
                        USE_OPENAI = True
                        
                        # 初始化OpenAI客户端
                        openai.api_key = OPENAI_API_KEY
                        openai.api_base = OPENAI_API_BASE
                        
                        logger.info(f"OpenAI配置加载成功! API密钥前8位={OPENAI_API_KEY[:8]}..., 基础URL={OPENAI_API_BASE}, 模型={OPENAI_MODEL}")
                        logger.info(f"OpenAI客户端初始化完成")
                    else:
                        logger.warning("未找到有效的OpenAI API密钥")
                        USE_OPENAI = False
                except Exception as e:
                    logger.error(f"配置文件读取失败: {e}")
                    logger.error(traceback.format_exc())
                    USE_OPENAI = False
            else:
                logger.warning(f"配置文件不存在: {config_path}")
                USE_OPENAI = False
        except Exception as e:
            logger.error(f"加载OpenAI配置时出错: {e}")
            logger.error(traceback.format_exc())
            USE_OPENAI = False
            
        logger.info(f"OpenAI配置加载结果: {'成功' if USE_OPENAI else '失败'}")
        return USE_OPENAI

    def get_help_text(self, **kwargs):
        help_text = """【使用说明】
1. 基础查询（显示前10条）：
   - 票种 出发地 终点地 （例：高铁 北京 上海）
   - 票种 出发地 终点地 日期 （例：高铁 北京 上海 2024-06-05）
   - 票种 出发地 终点地 日期 时间 （例：高铁 北京 上海 2024-06-05 09:00）

2. 自然语言查询：
   - "查明天上午从北京到上海的高铁"
   - "今天下午3点的高铁从北京到上海"
   
3. 分页操作：
   - +下一页：查看后续结果
   - +上一页：返回前页结果

4. 后续筛选：
   - +最便宜的二等座
   - +上午出发的车次

5. 中转查询：
   - 中转+高铁 成都 上海 2024-06-05 09:00"""
        return help_text

    def on_handle_context(self, e_context: EventContext):
        if e_context['context'].type != ContextType.TEXT:
            return
            
        self.content = e_context["context"].content.strip()
        logger.info(f"收到查询内容：{self.content}")

        # 处理分页命令
        if self.content in ["+下一页", "+上一页"]:
            self._handle_pagination(e_context)
            return

        # 处理后续筛选问题
        if self.content.startswith("+"):
            logger.info("开始处理后续筛选问题")
            self._handle_followup_question(e_context)
            return

        # 清理10分钟前的历史记录
        if self.last_interaction_time and datetime.now() - self.last_interaction_time > timedelta(minutes=10):
            self.conversation_history.clear()
            self.total_data = []
            self.current_page = 1
            logger.info("已清除过期对话历史")

        self.last_interaction_time = datetime.now()

        # 处理中转查询
        if self.content.startswith("中转+") or "中转" in self.content or "换乘" in self.content:
            logger.info("检测到中转查询请求")
            self._handle_transfer_query(e_context)
            return

        # 检查是否是自然语言查询
        if any(keyword in self.content for keyword in ["高铁", "动车", "普通"]) and any(keyword in self.content for keyword in ["到", "去", "至"]):
            logger.info("检测到自然语言查询，开始处理")
            
            # 首先尝试使用OpenAI进行处理
            if USE_OPENAI:
                logger.info("====== 使用OpenAI处理自然语言查询 ======")
                logger.info(f"API密钥状态: {'已配置' if OPENAI_API_KEY else '未配置'}")
                logger.info(f"API基础URL: {OPENAI_API_BASE}")
                logger.info(f"使用模型: {OPENAI_MODEL}")
                
                # 尝试调用OpenAI解析
                parsed_query = self._ai_parse_query(self.content)
                if parsed_query:
                    logger.info(f"OpenAI成功解析查询: {parsed_query}")
                    self.content = parsed_query
                    self._handle_main_query(e_context)
                    return
                else:
                    logger.warning("OpenAI解析失败，回退到正则表达式解析")
            else:
                logger.warning("OpenAI未配置，使用正则表达式解析")
                
            # 如果OpenAI未配置或调用失败，回退到正则表达式解析
            self._process_natural_language()
            # 自然语言处理后直接交给主查询处理
            self._handle_main_query(e_context)
            return

        # 处理主查询
        if self.content.split()[0] in ["高铁", "普通", "动车"]:
            logger.info("开始处理主查询")
            self._handle_main_query(e_context)

    def _process_natural_language(self):
        """处理自然语言查询"""
        try:
            logger.info(f"开始解析自然语言查询：{self.content}")
            
            # 1. 提取车型
            ticket_type = "高铁" if "高铁" in self.content else "动车" if "动车" in self.content else "普通"
            
            # 2. 提取城市 - 支持多种表达方式
            # 匹配模式1：从A到B
            location_pattern1 = r"从([\u4e00-\u9fa5]+)到([\u4e00-\u9fa5]+)"
            # 匹配模式2：A到B / A至B / A去B
            location_pattern2 = r"([\u4e00-\u9fa5]+)(?:到|至|去)([\u4e00-\u9fa5]+)"
            
            # 先处理日期和时间短语
            time_keywords = ["今天", "明天", "后天", "下午", "上午", "晚上", "凌晨", "中午", "早上"]
            cleaned_content = self.content
            for keyword in time_keywords:
                cleaned_content = cleaned_content.replace(keyword, " " + keyword + " ")
            
            logger.info(f"预处理后的查询内容：{cleaned_content}")
            
            # 在预处理后的内容中查找城市
            location_match = re.search(location_pattern1, cleaned_content)
            if not location_match:
                location_match = re.search(location_pattern2, cleaned_content)
                
            if not location_match:
                logger.warning("未找到出发地和目的地")
                return
                
            from_city = location_match.group(1).strip()
            to_city = location_match.group(2).strip()
            
            # 清除可能的额外文本和时间词
            for keyword in time_keywords:
                from_city = from_city.replace(keyword, "").strip()
                to_city = to_city.replace(keyword, "").strip()
                
            # 清除可能的额外文本
            to_city = to_city.split("的")[0].strip() if "的" in to_city else to_city
            
            logger.info(f"识别到城市：{from_city} -> {to_city}")
            
            # 3. 处理时间
            now = datetime.now()
            query_date = now.strftime("%Y-%m-%d")  # 默认今天
            query_time = None
            
            # 处理日期
            if "明天" in self.content:
                query_date = (now + timedelta(days=1)).strftime("%Y-%m-%d")
                logger.info(f"识别到日期：明天 ({query_date})")
            elif "后天" in self.content:
                query_date = (now + timedelta(days=2)).strftime("%Y-%m-%d")
                logger.info(f"识别到日期：后天 ({query_date})")
            else:
                logger.info(f"使用默认日期：今天 ({query_date})")
                
            # 处理具体时间
            time_pattern = r"(\d{1,2})(?:点|时|:|：)(\d{0,2})(?:分|)|(\d{1,2})(?:点|时)"
            time_match = re.search(time_pattern, self.content)
            
            if time_match:
                if time_match.group(3):  # 匹配了"3点"这种格式
                    hour = int(time_match.group(3))
                    minute = 0
                else:  # 匹配了"3:30"或"3点30分"这种格式
                    hour = int(time_match.group(1))
                    minute = int(time_match.group(2)) if time_match.group(2) else 0
                
                # 处理12小时制转24小时制
                if "下午" in self.content or "晚上" in self.content:
                    if hour < 12:
                        hour += 12
                        
                query_time = f"{hour:02d}:{minute:02d}"
                logger.info(f"提取到具体时间：{query_time}")
                
                # 处理"左右"、"前后"等模糊表达，设置时间窗口
                if any(word in self.content for word in ["左右", "前后", "附近"]):
                    # 这里我们不改变query_time，但记录这是一个近似时间
                    # 后续处理中会使用一个时间窗口，比如±30分钟
                    self.is_approximate_time = True
                    self.approximate_time = query_time
                    logger.info(f"检测到模糊时间表达，将使用{query_time}±30分钟的时间窗口")
            
            # 如果没有提取到具体时间，则尝试提取时间段
            if not query_time:
                if "上午" in self.content:
                    query_time = "09:00"
                    logger.info("识别到时间段：上午，设置为09:00")
                elif "下午" in self.content and "晚" not in self.content:
                    query_time = "14:00"
                    logger.info("识别到时间段：下午，设置为14:00")
                elif "晚上" in self.content or "傍晚" in self.content:
                    query_time = "19:00"
                    logger.info("识别到时间段：晚上，设置为19:00")
            
            # 4. 构建查询参数
            parts = [ticket_type, from_city, to_city]
            if query_date:
                parts.append(query_date)
            if query_time:
                parts.append(query_time)
                
            self.content = " ".join(parts)
            logger.info(f"解析结果：{self.content}")
            
            # 5. 记录原始查询，用于后续精确过滤
            self.original_query = self.content
            if "左右" in self.content or "前后" in self.content or "附近" in self.content:
                self.is_approximate_time = True
                self.approximate_time = query_time
            else:
                self.is_approximate_time = False
                
        except Exception as e:
            logger.error(f"自然语言解析失败：{e}")
            logger.error(traceback.format_exc())

    def _handle_main_query(self, e_context):
        """处理主查询请求"""
        logger.info(f"处理主查询：{self.content}")
        parts = self.content.split()
        
        # 检查参数数量
        if len(parts) < 3:
            self._send_error("参数不足，请至少提供车型、出发地和目的地", e_context)
            return

        try:
            # 解析基础参数
            ticket_type = parts[0]
            from_location = parts[1]
            to_location = parts[2]
            
            # 获取当前时间
            now = datetime.now()
            query_date = now.strftime("%Y-%m-%d")
            query_time = None
            
            # 处理可选参数
            if len(parts) >= 4:
                if re.match(r"\d{4}-\d{2}-\d{2}", parts[3]):
                    query_date = parts[3]
                elif re.match(r"\d{1,2}:\d{2}", parts[3]):
                    query_time = parts[3]
                    
            if len(parts) >= 5:
                if re.match(r"\d{1,2}:\d{2}", parts[4]):
                    query_time = parts[4]

            # 记录查询参数
            logger.info(f"查询参数：车型={ticket_type}, 出发={from_location}, 到达={to_location}, "
                       f"日期={query_date}, 时间={query_time or '全天'}")

            # 获取票务信息
            result = self.get_ticket_info(
                ticket_type, 
                from_location, 
                to_location,
                query_date,
                query_time
            )
            
            if result:
                # 保存完整数据用于分页
                self.total_data = result
                self.last_query_params = (ticket_type, from_location, to_location, query_date, query_time)
                self.current_page = 1
                
                # 显示第一页结果
                reply = Reply()
                reply.type = ReplyType.TEXT
                reply.content = self._format_response(self._get_current_page())
            else:
                reply = Reply()
                reply.type = ReplyType.ERROR
                reply.content = "未找到符合条件的车次"
                
            e_context["reply"] = reply
            e_context.action = EventAction.BREAK_PASS

        except Exception as e:
            logger.error(f"处理查询时发生错误：{e}")
            logger.error(traceback.format_exc())
            self._send_error("查询处理失败，请稍后重试", e_context)

    def get_ticket_info(self, ticket_type, from_loc, to_loc, date, time=""):
        """调用票务API获取数据"""
        logger.info(f"开始查询车票信息：{ticket_type} {from_loc}->{to_loc} 日期：{date} 时间：{time}")
        
        # 保存时间信息用于后续过滤
        if time:
            self.is_approximate_time = True
            self.approximate_time = time
            logger.info(f"设置近似时间过滤条件：{time}±30分钟")
        
        # 构建查询参数
        params = {
            "from": from_loc,
            "to": to_loc,
            "time": date,  # API参数为time而不是date
            "type": ticket_type
        }
        
        # 输出完整请求URL
        param_str = "&".join([f"{k}={v}" for k, v in params.items()])
        full_url = f"{BASE_URL_HIGHSPEEDTICKET}?{param_str}"
        logger.info(f"请求URL：{full_url}")
        
        try:
            resp = requests.get(BASE_URL_HIGHSPEEDTICKET, params=params, timeout=15)
            logger.info(f"API响应状态码：{resp.status_code}")
            logger.info(f"API响应内容：{resp.text[:200]}...")  # 只输出前200个字符避免日志过长
            
            if resp.status_code != 200:
                logger.error(f"API请求失败，状态码：{resp.status_code}")
                return None
                
            try:
                data = resp.json()
                logger.info(f"API返回code：{data.get('code')}")
                logger.info(f"API返回msg：{data.get('msg')}")
                
                if data.get('code') == 200:
                    raw_data = data.get('data', [])
                    logger.info(f"获取到{len(raw_data)}条原始数据")
                    
                    # 处理数据前先输出几条样例
                    if raw_data:
                        logger.info(f"数据样例：{raw_data[0]}")
                    
                    filtered_trains = self._process_api_data(raw_data, ticket_type, time)
                    logger.info(f"筛选后剩余{len(filtered_trains)}条数据")
                    
                    if not filtered_trains:
                        logger.warning("筛选后没有符合条件的车次")
                    return filtered_trains
                else:
                    error_msg = data.get('msg', '未知错误')
                    logger.error(f"API返回错误：{error_msg}")
                    return None
                    
            except json.JSONDecodeError as je:
                logger.error(f"JSON解析错误：{je}")
                logger.error(f"原始响应内容：{resp.text}")
                return None
                
        except requests.exceptions.Timeout:
            logger.error("API请求超时")
            return None
        except requests.exceptions.RequestException as e:
            logger.error(f"请求异常：{e}")
            return None
        except Exception as e:
            logger.error(f"未知错误：{str(e)}")
            logger.error(f"错误详情：{traceback.format_exc()}")
            return None

    def _process_api_data(self, data, ticket_type, query_time):
        """处理API返回数据"""
        logger.info(f"开始处理API数据：车型={ticket_type}, 查询时间={query_time}")
        logger.info(f"收到{len(data)}条数据待处理")
        
        # 记录时间过滤状态
        if self.is_approximate_time:
            logger.info(f"启用近似时间过滤：{self.approximate_time}±30分钟")
        elif query_time:
            logger.info(f"启用精确时间过滤：{query_time}之后的车次")
        else:
            logger.info("未指定时间过滤条件，将返回全天车次")
        
        filtered = []
        for item in data:
            try:
                # 记录每条数据的处理
                train_number = item.get('trainumber', 'unknown')
                train_type = item.get('traintype', 'unknown')
                depart_time = item.get('departtime', 'unknown')
                
                logger.info(f"处理车次：{train_number} 类型：{train_type} 发车：{depart_time}")
                
                # 1. 车型筛选
                if train_type != ticket_type:
                    logger.debug(f"车次{train_number}类型不匹配，跳过")
                    continue
                    
                # 2. 时间筛选
                # 如果是近似时间查询（例如"10点左右"），使用时间窗口
                if self.is_approximate_time and self.approximate_time:
                    try:
                        # 解析近似时间和发车时间
                        approx_time_obj = datetime.strptime(self.approximate_time, "%H:%M").time()
                        depart_time_obj = datetime.strptime(depart_time, "%H:%M").time()
                        
                        # 计算时间差（分钟）
                        approx_minutes = approx_time_obj.hour * 60 + approx_time_obj.minute
                        depart_minutes = depart_time_obj.hour * 60 + depart_time_obj.minute
                        time_diff = abs(approx_minutes - depart_minutes)
                        
                        # 使用30分钟的时间窗口
                        if time_diff > 30:
                            logger.info(f"车次{train_number}发车时间{depart_time}与近似时间{self.approximate_time}相差{time_diff}分钟，超出30分钟窗口，跳过")
                            continue
                        else:
                            logger.info(f"✓ 车次{train_number}发车时间{depart_time}在近似时间{self.approximate_time}的30分钟窗口内")
                    except ValueError as e:
                        logger.warning(f"近似时间格式解析错误: {e}")
                        
                # 常规时间筛选
                elif query_time:
                    try:
                        query_time_obj = datetime.strptime(query_time, "%H:%M").time()
                        depart_time_obj = datetime.strptime(depart_time, "%H:%M").time()
                        
                        # 计算时间差（分钟）
                        query_minutes = query_time_obj.hour * 60 + query_time_obj.minute
                        depart_minutes = depart_time_obj.hour * 60 + depart_time_obj.minute
                        time_diff = depart_minutes - query_minutes
                        
                        if time_diff < -30:  # 发车时间早于查询时间30分钟以上
                            logger.info(f"车次{train_number}发车时间{depart_time}早于查询时间{query_time}超过30分钟，跳过")
                            continue
                        else:
                            logger.info(f"✓ 车次{train_number}发车时间{depart_time}接近或晚于查询时间{query_time}")
                    except ValueError as e:
                        logger.warning(f"时间格式解析错误: {e}")
                        continue
                        
                # 3. 添加有效数据
                filtered.append(item)
                logger.info(f"✅ 添加符合条件的车次：{train_number}, 发车时间：{depart_time}, 到达时间：{item.get('arrivetime', 'unknown')}")
                          
            except KeyError as ke:
                logger.warning(f"数据格式错误，缺少必要字段：{ke}")
                continue
            except Exception as e:
                logger.warning(f"处理数据时发生错误：{e}")
                continue

        # 按发车时间排序
        filtered.sort(key=lambda x: x['departtime'])
        logger.info(f"筛选完成，共有{len(filtered)}条符合条件的车次")
        
        # 输出筛选后的第一条数据作为样例
        if filtered:
            logger.info(f"筛选后数据样例：{filtered[0]}")
        
        return filtered
        
    def _handle_pagination(self, e_context):
        """处理分页请求"""
        if not self.total_data:
            self._send_error("请先进行车次查询", e_context)
            return

        # 计算总页数
        total_pages = (len(self.total_data) + self.page_size - 1) // self.page_size

        if self.content == "+下一页":
            if self.current_page < total_pages:
                self.current_page += 1
            else:
                self._send_error("已经是最后一页了", e_context)
                return
        elif self.content == "+上一页":
            if self.current_page > 1:
                self.current_page -= 1
            else:
                self._send_error("已经是第一页了", e_context)
                return

        # 获取当前页数据
        page_data = self._get_current_page()
        reply = Reply()
        reply.type = ReplyType.TEXT
        reply.content = self._format_response(page_data)
        e_context["reply"] = reply
        e_context.action = EventAction.BREAK_PASS

    def _get_current_page(self):
        """获取当前页数据"""
        start = (self.current_page - 1) * self.page_size
        end = start + self.page_size
        return self.total_data[start:end]
        
    def _format_response(self, page_data):
        if not page_data:
            return "没有更多车次信息"

        # 限制最大显示结果，避免消息过长
        if len(page_data) > 20:
            logger.warning(f"结果过多({len(page_data)}条)，只显示前20条")
            page_data = page_data[:20]

        result = []
        global_index = (self.current_page - 1) * self.page_size + 1
        for idx, item in enumerate(page_data, global_index):
            info = f"{idx}. 【{item.get('trainumber', '未知车次')}】{item.get('traintype', '未知类型')}\n"
            info += f"   🚩出发站：{item.get('departstation', '未知')} ➔ 到达站：{item.get('arrivestation', '未知')}\n"
            info += f"   ⏰时间：{item.get('departtime', '未知')} - {item.get('arrivetime', '未知')}（历时：{item.get('runtime', '未知')}\n"
            
            # 处理票价信息
            seats = item.get('ticket_info', [])
            if seats:
                seat_info = "   💺席位："
                seat_info += " | ".join([
                    f"{s.get('seatname', '未知')}：¥{s.get('seatprice', '未知')}（余{s.get('seatinventory', 0)}张）"
                    for s in seats
                ])
                info += seat_info + "\n"
            else:
                info += "   ⚠️暂无余票信息\n"
            
            result.append(info)
            
        total_pages = (len(self.total_data) + self.page_size - 1) // self.page_size
        footer = f"\n📄第 {self.current_page}/{total_pages} 页"
        footer += f"\n🔍共找到 {len(self.total_data)} 条符合条件的车次"
        footer += "\n🔍发送【+下一页】查看后续结果" if self.current_page < total_pages else ""
        footer += "\n🎯发送【+筛选条件】进行精确筛选（如：+二等座低于500元）"
        return "\n".join(result) + footer

    def _handle_followup_question(self, e_context):
        """处理后续筛选问题"""
        content = self.content[1:]  # 去掉开头的"+"
        logger.info(f"收到筛选问题：+{content}")
        
        # 检查是否有查询结果
        if not self.original_data:
            self._send_error("请先进行车次查询", e_context)
            return
            
        # 判断是否需要使用OpenAI进行智能筛选
        if USE_OPENAI:
            logger.info("====== 使用OpenAI进行智能筛选 ======")
            logger.info(f"API密钥前8位: {OPENAI_API_KEY[:8] if OPENAI_API_KEY else '未配置'}")
            logger.info(f"API基础URL: {OPENAI_API_BASE}")
            logger.info(f"使用模型: {OPENAI_MODEL}")
            
            # 判断是否正在处理中转查询结果
            if hasattr(self, 'is_transfer_query') and self.is_transfer_query:
                logger.info("检测到正在处理中转查询结果，使用中转筛选流程")
                filtered_data = self._ai_filter_transfer(content)
            else:
                logger.info("使用普通查询筛选流程")
                filtered_data = self._ai_filter(content)
        else:
            logger.info("OpenAI未配置，使用本地筛选逻辑")
            
            # 判断是否正在处理中转查询结果
            if hasattr(self, 'is_transfer_query') and self.is_transfer_query:
                logger.info("检测到正在处理中转查询结果，使用中转筛选流程")
                filtered_data = self._manual_filter_transfer(content)
            else:
                logger.info("使用普通查询筛选流程")
                filtered_data = self._manual_filter(content)
        
        # 更新现有数据 - 只更新total_data，保留original_data
        if filtered_data is not None:
            if len(filtered_data) > 0:
                self.total_data = filtered_data
                self.current_page = 1
                
                # 格式化响应
                if hasattr(self, 'is_transfer_query') and self.is_transfer_query:
                    reply_content = self._format_transfer_response(filtered_data[:20])  # 限制显示条数
                else:
                    page_data = self._get_current_page()
                    reply_content = self._format_response(page_data)
                
                reply = Reply()
                reply.type = ReplyType.TEXT
                reply.content = reply_content
                e_context["reply"] = reply
                e_context.action = EventAction.BREAK_PASS
            else:
                self._send_error("未找到符合条件的车次", e_context)
        else:
            self._send_error("筛选失败，请重试", e_context)

    def _ai_filter_transfer(self, question):
        """针对中转查询结果的AI筛选"""
        logger.info(f"使用AI筛选中转查询结果: {question}")
        
        if not USE_OPENAI or not OPENAI_API_KEY:
            logger.warning("OpenAI配置无效，回退到手动筛选")
            return self._manual_filter_transfer(question)
            
        try:
            # 配置OpenAI
            logger.info(f"初始化OpenAI客户端...")
            logger.info(f"API密钥: {OPENAI_API_KEY[:8]}...")
            logger.info(f"API基础URL: {OPENAI_API_BASE}")
            logger.info(f"使用模型: {OPENAI_MODEL}")
            
            # 强制重新配置OpenAI
            openai.api_key = OPENAI_API_KEY
            openai.api_base = OPENAI_API_BASE
            
            # 准备数据，始终使用原始数据，限制数量防止超出API限制
            max_data_items = min(len(self.original_data), 20)
            sample_data = self.original_data[:max_data_items]
            
            # 构建简化的样本数据以适应token限制
            simplified_samples = []
            for route in sample_data:
                simplified = {
                    "transfer_station": route.get("transfer_station"),
                    "total_price": route.get("total_price"),
                    "total_runtime": route.get("total_runtime"),
                    "first_leg": {
                        "trainumber": route.get("first_leg", {}).get("trainumber"),
                        "traintype": route.get("first_leg", {}).get("traintype"),
                        "departtime": route.get("first_leg", {}).get("departtime"),
                        "arrivetime": route.get("first_leg", {}).get("arrivetime"),
                        "departstation": route.get("first_leg", {}).get("departstation"),
                        "ticket_info": [
                            {
                                "seatname": seat.get("seatname"),
                                "seatprice": seat.get("seatprice"),
                                "seatinventory": seat.get("seatinventory")
                            } for seat in route.get("first_leg", {}).get("ticket_info", [])[:2]  # 只包含前两种座位类型
                        ]
                    },
                    "second_leg": {
                        "trainumber": route.get("second_leg", {}).get("trainumber"),
                        "traintype": route.get("second_leg", {}).get("traintype"),
                        "departtime": route.get("second_leg", {}).get("departtime"),
                        "arrivetime": route.get("second_leg", {}).get("arrivetime"),
                        "arrivestation": route.get("second_leg", {}).get("arrivestation"),
                        "ticket_info": [
                            {
                                "seatname": seat.get("seatname"),
                                "seatprice": seat.get("seatprice"),
                                "seatinventory": seat.get("seatinventory")
                            } for seat in route.get("second_leg", {}).get("ticket_info", [])[:2]  # 只包含前两种座位类型
                        ]
                    },
                    "transfer_time": route.get("transfer_time"),
                    "index": sample_data.index(route)  # 添加索引以便后续查找
                }
                simplified_samples.append(simplified)
            
            sample_json = json.dumps(simplified_samples, ensure_ascii=False)
            logger.info(f"已准备{len(simplified_samples)}/{len(self.original_data)}条中转数据用于AI分析")
            
            # 构建提示
            prompt = f"""
            我需要按以下条件筛选中转列车方案: "{question}"
            
            中转方案数据格式示例：
            {sample_json}
            
            请分析筛选条件，并返回符合条件的中转方案。返回格式为JSON：
            {{
                "analysis": "对筛选条件的理解和分析...",
                "matched_routes": [0, 2, 5]  // 匹配的方案在原数组中的索引
            }}
            
            如果筛选条件涉及总价格，请查看total_price字段；
            如果涉及总时间，请查看total_runtime字段（以分钟为单位）；
            如果涉及车次号，请查看first_leg和second_leg中的trainumber字段；
            如果涉及座位类型和价格，请查看ticket_info数组。
            如果涉及中转站，请查看transfer_station字段,只有完全匹配才算符合条件。
            
            仅返回JSON，不要有其他文字。
            """
            
            # 调用OpenAI API
            logger.info(f"正在调用OpenAI API - 使用模型: {OPENAI_MODEL}")
            
            try:
                # 尝试多种API调用方式
                result_text = ""
                
                try:
                    # 新版API
                    response = openai.ChatCompletion.create(
                        model=OPENAI_MODEL,
                        messages=[{"role": "user", "content": prompt}],
                        temperature=0.3,
                        max_tokens=1000
                    )
                    result_text = response.choices[0].message.content.strip()
                except AttributeError:
                    try:
                        # 最新客户端格式
                        response = openai.chat.completions.create(
                            model=OPENAI_MODEL,
                            messages=[{"role": "user", "content": prompt}],
                            temperature=0.3,
                            max_tokens=1000
                        )
                        result_text = response.choices[0].message.content.strip()
                    except Exception as latest_error:
                        # 旧版API
                        response = openai.Completion.create(
                            model=OPENAI_MODEL,
                            prompt=prompt,
                            temperature=0.3,
                            max_tokens=1000
                        )
                        result_text = response.choices[0].text.strip()
            
                # 处理API响应
                if not result_text:
                    logger.warning("OpenAI返回了空响应")
                    return self._manual_filter_transfer(question)
                    
                logger.info(f"OpenAI返回响应长度: {len(result_text)} 字符")
                
                # 去除markdown格式
                if result_text.startswith("```"):
                    pattern = r"```(?:json)?\s*([\s\S]*?)```"
                    match = re.search(pattern, result_text)
                    if match:
                        result_text = match.group(1).strip()
                
                # 解析JSON响应
                result_json = json.loads(result_text)
                indices = result_json.get("matched_routes", [])
                logger.info(f"AI分析: {result_json.get('analysis', '无分析')[:100]}...")
                logger.info(f"匹配索引: {indices}")
                
                # 根据索引筛选 - 使用全部原始数据
                if indices:
                    # 需要确保索引有效
                    valid_indices = [i for i in indices if 0 <= i < len(self.original_data)]
                    filtered = [self.original_data[i] for i in valid_indices]
                    logger.info(f"筛选结果: 保留{len(filtered)}/{len(self.original_data)}条中转方案")
                    
                    # 根据筛选条件确定排序方式
                    if any(word in question for word in ["最便宜", "价格最低", "便宜", "低价", "最低", "总票价"]):
                        logger.info("检测到价格相关筛选条件，对结果按价格排序")
                        filtered.sort(key=lambda x: float(x.get('total_price', float('inf'))))
                    elif any(word in question for word in ["最快", "时间最短", "耗时最少", "最短", "总时长"]):
                        logger.info("检测到时间相关筛选条件，对结果按时间排序")
                        filtered.sort(key=lambda x: int(x.get('total_runtime', float('inf'))))
                        
                    # 如果是要求最便宜/最快的一个，只返回第一个结果
                    if "最" in question and filtered:
                        if any(word in question for word in ["最便宜", "价格最低", "最低", "总票价最低"]):
                            logger.info(f"根据'最便宜'条件，只返回价格最低的方案: {filtered[0].get('total_price')}元")
                            return [filtered[0]]
                        elif any(word in question for word in ["最快", "时间最短", "耗时最少", "总时长最短"]):
                            logger.info(f"根据'最快'条件，只返回时间最短的方案: {filtered[0].get('total_runtime')}分钟")
                            return [filtered[0]]
                    
                    return filtered
                else:
                    # 如果AI无法找到匹配的，回退到手动筛选
                    logger.warning("AI未找到匹配的中转方案，尝试手动筛选")
                    return self._manual_filter_transfer(question)
                    
            except Exception as api_error:
                logger.error(f"API调用或解析失败: {api_error}")
                logger.error(traceback.format_exc())
                return self._manual_filter_transfer(question)
                
        except Exception as e:
            logger.error(f"AI筛选中转查询失败: {e}")
            logger.error(traceback.format_exc())
            return self._manual_filter_transfer(question)

    def _manual_filter_transfer(self, question):
        """针对中转查询结果的手动筛选"""
        logger.info(f"手动筛选中转查询结果: {question}")
        
        # 始终使用原始数据作为筛选基础
        data_to_filter = self.original_data
        logger.info(f"基于{len(data_to_filter)}条原始数据进行筛选")
        
        # 筛选逻辑 - 中转站相关
        if any(station in question for station in MAJOR_STATIONS):
            logger.info("检测到中转站相关筛选条件")
            
            # 提取指定的中转站
            specified_station = None
            for station in MAJOR_STATIONS:
                if station in question:
                    specified_station = station
                    logger.info(f"识别到筛选条件中的中转站: {station}")
                    break
                    
            if specified_station:
                logger.info(f"筛选中转站为{specified_station}的方案")
                filtered = []
                for route in data_to_filter:
                    station = route.get('transfer_station')
                    logger.info(f"检查路线中转站: {station}")
                    if station == specified_station:
                        filtered.append(route)
                
                logger.info(f"找到{len(filtered)}个经过{specified_station}的中转方案")
                return filtered
        
        # 筛选逻辑 - 价格相关
        elif any(word in question for word in ["最便宜", "价格最低", "便宜", "低价", "最低", "总票价"]):
            logger.info("检测到价格相关筛选条件")
            
            # 按总价排序
            sorted_routes = sorted(data_to_filter, key=lambda x: float(x.get('total_price', float('inf'))))
            logger.info(f"按总价排序完成，前3个方案的价格: " + 
                       ", ".join([f"{route.get('total_price', 'N/A')}元" for route in sorted_routes[:3]]))
            
            # 是否只返回最低价
            if any(word in question for word in ["最便宜", "价格最低", "最低", "总票价最低"]):
                if sorted_routes:
                    logger.info(f"找到最便宜的中转方案，总价: {sorted_routes[0].get('total_price')}元")
                    return [sorted_routes[0]]
                else:
                    return []
            else:
                logger.info(f"按总价排序，找到{len(sorted_routes)}个方案")
                return sorted_routes
                
        # 筛选逻辑 - 时间相关
        elif any(word in question for word in ["最快", "时间最短", "耗时最少", "最短", "总时长"]):
            logger.info("检测到时间相关筛选条件")
            
            # 按总时间排序
            sorted_routes = sorted(data_to_filter, key=lambda x: int(x.get('total_runtime', float('inf'))))
            logger.info(f"按总时长排序完成，前3个方案的时长(分钟): " + 
                       ", ".join([f"{route.get('total_runtime', 'N/A')}" for route in sorted_routes[:3]]))
            
            # 是否只返回最快的
            if any(word in question for word in ["最快", "时间最短", "耗时最少", "总时长最短"]):
                if sorted_routes:
                    total_minutes = sorted_routes[0].get('total_runtime', 0)
                    hours = total_minutes // 60
                    mins = total_minutes % 60
                    logger.info(f"找到最快的中转方案，总时长: {hours}小时{mins}分钟")
                    return [sorted_routes[0]]
                else:
                    return []
            else:
                logger.info(f"按总时长排序，找到{len(sorted_routes)}个方案")
                return sorted_routes
                
        # 筛选逻辑 - 换乘时间相关
        elif any(word in question for word in ["换乘时间", "中转时间", "等待时间"]):
            logger.info("检测到换乘时间相关筛选条件")
            
            # 是否要求最短换乘时间
            if any(word in question for word in ["最短", "最少"]):
                sorted_routes = sorted(data_to_filter, key=lambda x: int(x.get('transfer_time', float('inf'))))
                if sorted_routes:
                    logger.info(f"找到换乘时间最短的方案: {sorted_routes[0].get('transfer_time')}分钟")
                    return [sorted_routes[0]]
                
            # 是否要求最长换乘时间（可能是为了在中转站游玩）
            elif any(word in question for word in ["最长", "最多"]):
                sorted_routes = sorted(data_to_filter, key=lambda x: int(x.get('transfer_time', 0)), reverse=True)
                if sorted_routes:
                    logger.info(f"找到换乘时间最长的方案: {sorted_routes[0].get('transfer_time')}分钟")
                    return [sorted_routes[0]]
        
        # 筛选逻辑 - 车次号相关
        elif "车次" in question or "班次" in question:
            for route in data_to_filter:
                first_train = route.get('first_leg', {}).get('trainumber', '')
                second_train = route.get('second_leg', {}).get('trainumber', '')
                
                if first_train in question or second_train in question:
                    filtered.append(route)
                    
            if filtered:
                logger.info(f"按车次号筛选，找到{len(filtered)}个匹配方案")
                return filtered
        
        # 如果所有条件都不匹配，尝试使用更一般化的关键词匹配
        if "线路" in question or "方案" in question:
            if "最低" in question or "最便宜" in question:
                logger.info("检测到通用价格相关筛选条件")
                sorted_routes = sorted(data_to_filter, key=lambda x: float(x.get('total_price', float('inf'))))
                
                if "最" in question:
                    if sorted_routes:
                        logger.info(f"找到最便宜的中转方案，总价: {sorted_routes[0].get('total_price')}元")
                        return [sorted_routes[0]]
                    else:
                        return []
                else:
                    return sorted_routes
            
            # 处理"中转"或"经过"等关键词
            elif "中转" in question or "经过" in question:
                for station in MAJOR_STATIONS:
                    if station in question:
                        logger.info(f"检测到通用中转站筛选条件: {station}")
                        filtered = [route for route in data_to_filter if route.get('transfer_station') == station]
                        if filtered:
                            logger.info(f"找到{len(filtered)}个经过{station}的中转方案")
                            return filtered
        
        # 默认返回原始数据
        logger.info("未识别到明确的筛选条件，返回原始数据")
        return data_to_filter

    def _handle_transfer_query(self, e_context):
        """处理中转查询请求"""
        logger.info(f"开始处理中转查询: {self.content}")
        
        # 设置查询类型标记，以便后续筛选区分处理
        self.original_query = self.content
        self.is_transfer_query = True  # 标记为中转查询
        
        try:
            # 解析查询参数
            content = self.content
            
            # 解析自然语言中转查询
            is_natural_language = False
            if not content.startswith("中转+") and ("中转" in content or "换乘" in content):
                is_natural_language = True
                logger.info("检测到自然语言中转查询，开始解析")
                
                # 首先尝试使用OpenAI进行解析
                if USE_OPENAI and OPENAI_API_KEY:
                    logger.info("====== 尝试使用OpenAI解析中转查询 ======")
                    logger.info(f"API密钥状态: {'已配置' if OPENAI_API_KEY else '未配置'}")
                    logger.info(f"API基础URL: {OPENAI_API_BASE}")
                    logger.info(f"使用模型: {OPENAI_MODEL}")
                    
                    # 调用OpenAI解析
                    ai_result = self._ai_parse_transfer_query(content)
                    if ai_result:
                        logger.info("OpenAI成功解析中转查询")
                        ticket_type, from_loc, to_loc, query_date, query_time, user_specified_transfer = ai_result
                    else:
                        logger.warning("OpenAI解析失败，回退到正则表达式解析")
                        ticket_type, from_loc, to_loc, query_date, query_time, user_specified_transfer = self._parse_natural_transfer_query(content)
                else:
                    logger.info("OpenAI未配置或禁用，使用正则表达式解析")
                    ticket_type, from_loc, to_loc, query_date, query_time, user_specified_transfer = self._parse_natural_transfer_query(content)
                
                if ticket_type and from_loc and to_loc:
                    logger.info(f"自然语言解析结果: 车型={ticket_type}, 出发={from_loc}, 目的地={to_loc}, "
                              f"日期={query_date}, 时间={query_time or '全天'}, 指定中转站={user_specified_transfer or '无'}")
                else:
                    self._send_error("无法解析中转查询，请使用格式: 中转+高铁 上海 成都 2024-03-15", e_context)
                    return
            else:
                # 标准格式解析
                logger.info("使用标准格式解析中转查询")
                
                # 如果查询以"中转+"开头，去掉这个前缀
                if content.startswith("中转+"):
                    content = content[3:]
                
                # 检查用户是否指定了中转站
                user_specified_transfer = None
                for station in MAJOR_STATIONS:
                    if f"经{station}" in content or f"通过{station}" in content or f"从{station}中转" in content:
                        user_specified_transfer = station
                        logger.info(f"用户指定中转站: {station}")
                        content = content.replace(f"经{station}", "").replace(f"通过{station}", "").replace(f"从{station}中转", "")
                        break
                
                # 按照标准格式解析查询参数
                parts = content.strip().split()
                
                if len(parts) < 3:
                    self._send_error("中转查询参数不足，请至少提供车型、出发地和目的地", e_context)
                    return
                    
                # 解析参数
                ticket_type = parts[0]
                from_loc = parts[1]
                to_loc = parts[2]
                
                # 获取当前时间作为默认值
                now = datetime.now()
                query_date = now.strftime("%Y-%m-%d")
                query_time = None
                
                # 处理可选参数
                if len(parts) >= 4:
                    if re.match(r"\d{4}-\d{2}-\d{2}", parts[3]):
                        query_date = parts[3]
                    elif re.match(r"\d{1,2}:\d{2}", parts[3]):
                        query_time = parts[3]
                        
                if len(parts) >= 5:
                    if re.match(r"\d{1,2}:\d{2}", parts[4]):
                        query_time = parts[4]
            
            logger.info(f"中转查询参数: 车型={ticket_type}, 出发={from_loc}, 目的地={to_loc}, "
                      f"日期={query_date}, 时间={query_time or '全天'}")
            
            # 清空之前的查询结果
            self.original_data = []
            self.total_data = []
            self.current_page = 1
            
            # 确定中转站
            transfer_stations = self._find_transfer_stations(from_loc, to_loc, user_specified_transfer)
            if not transfer_stations:
                self._send_error(f"无法找到从{from_loc}到{to_loc}的合适中转站", e_context)
                return
                
            logger.info(f"将尝试以下中转站: {', '.join(transfer_stations)}")
            
            # 查询中转路线
            transfer_routes = self._search_transfer_routes(
                ticket_type, from_loc, to_loc, transfer_stations, query_date, query_time
            )
            
            if not transfer_routes:
                self._send_error(f"未找到从{from_loc}经中转站到{to_loc}的可行路线", e_context)
                return
                
            # 保存结果到original_data和total_data
            self.original_data = transfer_routes
            self.total_data = transfer_routes.copy()  # 使用副本，避免引用相同对象
            logger.info(f"已保存{len(transfer_routes)}条中转查询结果")
            
            # 格式化并发送响应
            reply = Reply()
            reply.type = ReplyType.TEXT
            reply.content = self._format_transfer_response(transfer_routes)
            e_context["reply"] = reply
            e_context.action = EventAction.BREAK_PASS
            
        except Exception as e:
            logger.error(f"处理中转查询时出错: {e}")
            logger.error(traceback.format_exc())
            self._send_error("中转查询处理失败，请稍后重试", e_context)

    def _ai_parse_query(self, query):
        """使用OpenAI解析自然语言查询"""
        if not USE_OPENAI or not OPENAI_API_KEY:
            logger.warning("OpenAI配置无效，无法使用AI解析")
            return None
            
        logger.info(f"开始使用OpenAI解析查询: {query}")
        
        try:
            # 强制重新配置OpenAI
            openai.api_key = OPENAI_API_KEY
            openai.api_base = OPENAI_API_BASE
            
            # 验证OpenAI配置
            logger.info(f"OpenAI配置验证 - API密钥前8位: {OPENAI_API_KEY[:8]}...")
            logger.info(f"OpenAI配置验证 - API基础URL: {OPENAI_API_BASE}")
            logger.info(f"OpenAI配置验证 - 模型: {OPENAI_MODEL}")
            
            # 获取当前日期信息，供提示中使用
            now = datetime.now()
            today_date = now.strftime("%Y-%m-%d")
            tomorrow_date = (now + timedelta(days=1)).strftime("%Y-%m-%d")
            day_after_tomorrow_date = (now + timedelta(days=2)).strftime("%Y-%m-%d")
            
            # 计算本周和下周的各天日期
            weekday_today = now.weekday()  # 0是周一，6是周日
            
            # 计算本周各天的日期
            this_week_dates = {}
            for i in range(7):
                day_offset = i - weekday_today  # 相对于今天的偏移
                date = (now + timedelta(days=day_offset)).strftime("%Y-%m-%d")
                this_week_dates[i] = date  # 0->周一, 1->周二, ...
                
            # 计算下周各天的日期
            next_week_dates = {}
            for i in range(7):
                day_offset = i - weekday_today + 7  # 加7表示下一周
                date = (now + timedelta(days=day_offset)).strftime("%Y-%m-%d")
                next_week_dates[i] = date
                
            # 当前日期信息
            current_date_info = f"""
            今天是 {today_date}，是{['周一', '周二', '周三', '周四', '周五', '周六', '周日'][weekday_today]}
            明天是 {tomorrow_date}
            后天是 {day_after_tomorrow_date}
            本周一是 {this_week_dates[0]}
            本周二是 {this_week_dates[1]}
            本周三是 {this_week_dates[2]}
            本周四是 {this_week_dates[3]}
            本周五是 {this_week_dates[4]}
            本周六是 {this_week_dates[5]}
            本周日是 {this_week_dates[6]}
            下周一是 {next_week_dates[0]}
            下周二是 {next_week_dates[1]}
            下周三是 {next_week_dates[2]}
            下周四是 {next_week_dates[3]}
            下周五是 {next_week_dates[4]}
            下周六是 {next_week_dates[5]}
            下周日是 {next_week_dates[6]}
            """
            
            # 构建提示
            prompt = f"""
            请分析以下高铁票查询请求，并提取出关键信息："{query}"
            
            请返回以下格式的结果（仅返回格式化结果，不要有其他解释）：
            车型 出发城市 目的城市 日期 [时间]
            
            当前日期信息：
            {current_date_info}
            
            请使用准确的日期（YYYY-MM-DD格式）：
            - 对于"今天"，使用 {today_date}
            - 对于"明天"，使用 {tomorrow_date}
            - 对于"后天"，使用 {day_after_tomorrow_date}
            - 对于"下周一"，使用 {next_week_dates[0]}
            - 对于"下周五"，使用 {next_week_dates[4]}
            
            示例查询和解析：
            查询："明天上海到北京的高铁"
            解析结果：高铁 上海 北京 {tomorrow_date}
            
            查询："后天下午3点从成都去重庆的动车"
            解析结果：动车 成都 重庆 {day_after_tomorrow_date} 15:00
            
            查询："下周三上午10点武汉到长沙的高铁"
            解析结果：高铁 武汉 长沙 {next_week_dates[2]} 10:00
            """
            
            # 输出请求信息
            logger.info(f"OpenAI请求：模型={OPENAI_MODEL}, prompt长度={len(prompt)}")
            
            # 尝试多种方式调用OpenAI API
            result_text = ""
            
            # 标准Python库方法失败后尝试直接使用requests调用API
            all_standard_methods_failed = False
            
            try:
                # 第一种方式：标准ChatCompletion API
                logger.info("尝试使用标准ChatCompletion API")
                response = openai.ChatCompletion.create(
                    model=OPENAI_MODEL,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.3,
                    max_tokens=50
                )
                logger.info("API调用成功!")
                result_text = response.choices[0].message.content.strip()
                
            except AttributeError as attr_error:
                logger.warning(f"标准API不可用: {attr_error}")
                
                try:
                    # 第二种方式：最新客户端格式
                    logger.info("尝试使用最新客户端格式")
                    response = openai.chat.completions.create(
                        model=OPENAI_MODEL,
                        messages=[{"role": "user", "content": prompt}],
                        temperature=0.3,
                        max_tokens=50
                    )
                    logger.info("最新客户端API调用成功!")
                    result_text = response.choices[0].message.content.strip()
                    
                except Exception as latest_error:
                    logger.warning(f"最新客户端API调用失败: {latest_error}")
                    
                    try:
                        # 第三种方式：旧版Completion API
                        logger.info("尝试使用旧版Completion API")
                        response = openai.Completion.create(
                            model=OPENAI_MODEL,
                            prompt=prompt,
                            temperature=0.3,
                            max_tokens=50
                        )
                        logger.info("旧版API调用成功!")
                        result_text = response.choices[0].text.strip()
                    except Exception as old_api_error:
                        logger.error(f"所有标准API调用方式均失败: {old_api_error}")
                        all_standard_methods_failed = True
            
            except Exception as api_error:
                logger.error(f"API调用失败: {api_error}")
                logger.error(traceback.format_exc())
                all_standard_methods_failed = True
                
            # 如果所有标准方法都失败，尝试直接使用requests
            if all_standard_methods_failed:
                logger.info("尝试使用直接HTTP请求调用OpenAI API")
                try:
                    # 构建请求URL - 确保URL格式正确
                    api_base = OPENAI_API_BASE
                    # 移除末尾的斜杠以避免双斜杠
                    if api_base.endswith("/"):
                        api_base = api_base[:-1]
                    
                    # 检查是否需要添加版本路径
                    if "v1" not in api_base.split("/")[-1]:
                        api_url = f"{api_base}/chat/completions"
                    else:
                        api_url = f"{api_base}/chat/completions"
                    
                    logger.info(f"请求URL: {api_url}")
                    
                    # 构建请求头
                    headers = {
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {OPENAI_API_KEY}"
                    }
                    
                    # 构建请求数据
                    payload = {
                        "model": OPENAI_MODEL,
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": 0.3,
                        "max_tokens": 50
                    }
                    
                    logger.info(f"发送HTTP请求到OpenAI API - 详细请求数据: {json.dumps(payload, ensure_ascii=False)}")
                    response = requests.post(api_url, headers=headers, json=payload, timeout=30)
                    
                    # 检查响应状态
                    logger.info(f"API响应状态码: {response.status_code}")
                    if response.status_code == 200:
                        response_json = response.json()
                        logger.info(f"API响应内容: {json.dumps(response_json, ensure_ascii=False)}")
                        result_text = response_json["choices"][0]["message"]["content"].strip()
                        logger.info(f"HTTP请求成功获取到结果: {result_text}")
                    else:
                        logger.error(f"HTTP请求失败: {response.text}")
                except Exception as req_error:
                    logger.error(f"HTTP请求出错: {req_error}")
                    logger.error(traceback.format_exc())
            
            if not result_text:
                logger.warning("OpenAI返回空结果")
                return None
                
            logger.info(f"OpenAI返回: {result_text}")
            
            # 验证结果格式
            parts = result_text.split()
            if len(parts) < 3:
                logger.warning(f"OpenAI返回格式不正确: {result_text}")
                return None
            
            # 确保至少包含车型、出发城市和目的城市
            logger.info(f"解析结果: 车型={parts[0]}, 出发城市={parts[1]}, 目的城市={parts[2]}")
            
            # 如果有日期部分，确保日期是正确的格式
            if len(parts) >= 4:
                date_part = parts[3]
                # 检查日期格式是否正确，如果不正确，尝试解析相对日期表达
                if not re.match(r"\d{4}-\d{2}-\d{2}", date_part):
                    # 使用我们已计算的日期信息作为备选方案
                    if "今天" in query:
                        parts[3] = today_date
                    elif "明天" in query:
                        parts[3] = tomorrow_date
                    elif "后天" in query:
                        parts[3] = day_after_tomorrow_date
                    elif "下周一" in query or "下礼拜一" in query:
                        parts[3] = next_week_dates[0]
                    elif "下周二" in query or "下礼拜二" in query:
                        parts[3] = next_week_dates[1]
                    elif "下周三" in query or "下礼拜三" in query:
                        parts[3] = next_week_dates[2]
                    elif "下周四" in query or "下礼拜四" in query:
                        parts[3] = next_week_dates[3]
                    elif "下周五" in query or "下礼拜五" in query:
                        parts[3] = next_week_dates[4]
                    elif "下周六" in query or "下礼拜六" in query:
                        parts[3] = next_week_dates[5]
                    elif "下周日" in query or "下礼拜日" in query or "下周天" in query:
                        parts[3] = next_week_dates[6]
                    elif "周一" in query or "礼拜一" in query:
                        parts[3] = this_week_dates[0]
                    elif "周二" in query or "礼拜二" in query:
                        parts[3] = this_week_dates[1]
                    elif "周三" in query or "礼拜三" in query:
                        parts[3] = this_week_dates[2]
                    elif "周四" in query or "礼拜四" in query:
                        parts[3] = this_week_dates[3]
                    elif "周五" in query or "礼拜五" in query:
                        parts[3] = this_week_dates[4]
                    elif "周六" in query or "礼拜六" in query:
                        parts[3] = this_week_dates[5]
                    elif "周日" in query or "礼拜日" in query or "周天" in query:
                        parts[3] = this_week_dates[6]
                    else:
                        parts[3] = today_date  # 默认使用今天
                    logger.info(f"修正日期为: {parts[3]}")
            
            # 返回修正后的格式化结果
            return " ".join(parts)
                
        except Exception as e:
            logger.error(f"OpenAI解析失败: {str(e)}")
            logger.error(traceback.format_exc())
            return None

    def _ai_parse_transfer_query(self, query):
        """使用OpenAI解析中转查询"""
        logger.info(f"使用OpenAI解析中转查询: {query}")
        
        try:
            # 配置OpenAI客户端
            logger.info(f"初始化OpenAI客户端...")
            logger.info(f"API密钥: {OPENAI_API_KEY[:8]}...")
            logger.info(f"API基础URL: {OPENAI_API_BASE}")
            logger.info(f"使用模型: {OPENAI_MODEL}")
            
            # 强制重新配置OpenAI
            openai.api_key = OPENAI_API_KEY
            openai.api_base = OPENAI_API_BASE
            
            # 获取当前日期
            now = datetime.now()
            today = now.strftime("%Y-%m-%d")
            tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%d")
            day_after_tomorrow = (now + timedelta(days=2)).strftime("%Y-%m-%d")
            
            # 构建提示
            prompt = f"""
            今天日期是 {today}。
            
            请解析以下中转查询，提取关键信息：
            "{query}"
            
            1. 车型（高铁/动车/普通，默认为高铁）
            2. 出发城市
            3. 目的地城市
            4. 日期（格式为YYYY-MM-DD，如果是"明天"则为 {tomorrow}，"后天"则为 {day_after_tomorrow}，如果未指定则默认为今天）
            5. 时间（如"上午9点"、"下午2点"等，如果未指定则为空）
            6. 指定中转站（如果用户指定了中转站，如"经武汉"、"通过郑州"等）
            
            返回JSON格式：
            {{
              "ticket_type": "高铁/动车/普通",
              "from_loc": "出发城市",
              "to_loc": "目的地城市", 
              "date": "YYYY-MM-DD",
              "time": "HH:MM或空",
              "transfer_station": "中转站或null"
            }}
            
            只返回JSON，不需要解释。如果无法解析某项，对应值设为null。
            """
            
            # 调用OpenAI API
            logger.info(f"正在调用OpenAI API - 使用模型: {OPENAI_MODEL}")
            
            try:
                # 新版API调用
                logger.info("尝试使用新版OpenAI API (ChatCompletion)")
                response = openai.ChatCompletion.create(
                    model=OPENAI_MODEL,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.3,
                    max_tokens=500
                )
                logger.info("新版API调用成功!")
                result_text = response.choices[0].message.content.strip()
                
            except AttributeError:
                try:
                    # 最新客户端格式
                    logger.info("尝试使用最新客户端格式")
                    response = openai.chat.completions.create(
                        model=OPENAI_MODEL,
                        messages=[{"role": "user", "content": prompt}],
                        temperature=0.3,
                        max_tokens=500
                    )
                    logger.info("最新客户端API调用成功!")
                    result_text = response.choices[0].message.content.strip()
                    
                except Exception as latest_error:
                    logger.warning(f"最新客户端API调用失败: {latest_error}")
                    logger.info("尝试使用旧版OpenAI API (Completion)")
                    response = openai.Completion.create(
                        model=OPENAI_MODEL,
                        prompt=prompt,
                        temperature=0.3,
                        max_tokens=500
                    )
                    logger.info("旧版API调用成功!")
                    result_text = response.choices[0].text.strip()
            
            # 检查并去除markdown代码块格式
            if result_text.startswith("```"):
                logger.info("检测到返回内容包含markdown代码块格式，正在移除...")
                pattern = r"```(?:json)?\s*([\s\S]*?)```"
                match = re.search(pattern, result_text)
                if match:
                    result_text = match.group(1).strip()
                    logger.info(f"移除markdown格式后的内容: {result_text[:100]}...")
            
            # 解析JSON响应
            result_json = json.loads(result_text)
            logger.info(f"OpenAI解析结果: {json.dumps(result_json, ensure_ascii=False)}")
            
            # 提取结果
            ticket_type = result_json.get("ticket_type")
            from_loc = result_json.get("from_loc")
            to_loc = result_json.get("to_loc")
            date = result_json.get("date")
            time = result_json.get("time")
            transfer_station = result_json.get("transfer_station")
            
            # 验证必要字段
            if ticket_type and from_loc and to_loc:
                logger.info("OpenAI成功解析出必要字段")
                return ticket_type, from_loc, to_loc, date, time, transfer_station
            else:
                logger.warning("OpenAI解析结果缺少必要字段")
                return None
                
        except Exception as e:
            logger.error(f"OpenAI解析中转查询失败: {e}")
            logger.error(traceback.format_exc())
            return None

    def _parse_natural_transfer_query(self, query):
        """解析自然语言中转查询"""
        logger.info(f"解析自然语言中转查询: {query}")
        
        # 默认值
        ticket_type = "高铁"  # 默认高铁
        from_loc = None
        to_loc = None
        query_date = None
        query_time = None
        user_specified_transfer = None
        
        try:
            # 1. 提取车型
            if "动车" in query:
                ticket_type = "动车"
            elif "普通" in query:
                ticket_type = "普通"
            
            # 2. 提取城市 - 支持多种表达方式
            # 匹配模式1：从A到B
            location_pattern1 = r"从([\u4e00-\u9fa5]+)到([\u4e00-\u9fa5]+)"
            # 匹配模式2：A到B / A至B / A去B
            location_pattern2 = r"([\u4e00-\u9fa5]+)(?:到|至|去)([\u4e00-\u9fa5]+)"
            
            # 先处理日期和时间短语
            time_keywords = ["今天", "明天", "后天", "下午", "上午", "晚上", "凌晨", "中午", "早上"]
            cleaned_content = query
            for keyword in time_keywords:
                cleaned_content = cleaned_content.replace(keyword, " " + keyword + " ")
            
            logger.info(f"预处理后的查询内容：{cleaned_content}")
            
            # 在预处理后的内容中查找城市
            location_match = re.search(location_pattern1, cleaned_content)
            if not location_match:
                location_match = re.search(location_pattern2, cleaned_content)
                
            if not location_match:
                logger.warning("未找到出发地和目的地")
                return None, None, None, None, None, None
                
            from_loc = location_match.group(1).strip()
            to_loc = location_match.group(2).strip()
            
            # 清除可能的额外文本和时间词
            for keyword in time_keywords:
                from_loc = from_loc.replace(keyword, "").strip()
                to_loc = to_loc.replace(keyword, "").strip()
                
            # 清除可能的额外文本
            to_loc = to_loc.split("的")[0].strip() if "的" in to_loc else to_loc
            
            logger.info(f"识别到城市：{from_loc} -> {to_loc}")
            
            # 3. 提取中转站
            for station in MAJOR_STATIONS:
                if f"经{station}" in query or f"通过{station}" in query or f"从{station}中转" in query or f"在{station}中转" in query:
                    user_specified_transfer = station
                    logger.info(f"识别到用户指定中转站: {station}")
                    break
            
            # 4. 处理时间
            now = datetime.now()
            query_date = now.strftime("%Y-%m-%d")  # 默认今天
            
            # 处理日期
            if "明天" in query:
                query_date = (now + timedelta(days=1)).strftime("%Y-%m-%d")
                logger.info(f"识别到日期：明天 ({query_date})")
            elif "后天" in query:
                query_date = (now + timedelta(days=2)).strftime("%Y-%m-%d")
                logger.info(f"识别到日期：后天 ({query_date})")
            else:
                logger.info(f"使用默认日期：今天 ({query_date})")
                
            # 处理具体时间
            time_pattern = r"(\d{1,2})(?:点|时|:|：)(\d{0,2})(?:分|)|(\d{1,2})(?:点|时)"
            time_match = re.search(time_pattern, query)
            
            if time_match:
                if time_match.group(3):  # 匹配了"3点"这种格式
                    hour = int(time_match.group(3))
                    minute = 0
                else:  # 匹配了"3:30"或"3点30分"这种格式
                    hour = int(time_match.group(1))
                    minute = int(time_match.group(2)) if time_match.group(2) else 0
                
                # 处理12小时制转24小时制
                if "下午" in query or "晚上" in query:
                    if hour < 12:
                        hour += 12
                        
                query_time = f"{hour:02d}:{minute:02d}"
                logger.info(f"提取到具体时间：{query_time}")
            
            # 如果没有提取到具体时间，则尝试提取时间段
            if not query_time:
                if "上午" in query:
                    query_time = "09:00"
                    logger.info("识别到时间段：上午，设置为09:00")
                elif "下午" in query and "晚" not in query:
                    query_time = "14:00"
                    logger.info("识别到时间段：下午，设置为14:00")
                elif "晚上" in query or "傍晚" in query:
                    query_time = "19:00"
                    logger.info("识别到时间段：晚上，设置为19:00")
            
            return ticket_type, from_loc, to_loc, query_date, query_time, user_specified_transfer
            
        except Exception as e:
            logger.error(f"自然语言中转查询解析失败：{e}")
            logger.error(traceback.format_exc())
            return None, None, None, None, None, None

    def _find_transfer_stations(self, from_loc, to_loc, user_specified=None):
        """确定中转站"""
        logger.info(f"寻找从{from_loc}到{to_loc}的中转站")
        
        # 1. 如果用户指定了中转站，优先使用
        if user_specified:
            logger.info(f"使用用户指定的中转站: {user_specified}")
            return [user_specified]
            
        # 2. 查找预定义的中转站
        key = (from_loc, to_loc)
        if key in TRANSFER_STATIONS:
            logger.info(f"使用预定义的中转站: {TRANSFER_STATIONS[key]}")
            return TRANSFER_STATIONS[key]
            
        # 3. 使用主要枢纽站作为候选中转站
        # 实际应用中，这里可以调用API查询更精确的中转站
        # 为了简化，这里暂时使用主要枢纽站中的前5个
        # 在实际实现中，应该根据地理位置和线路优化选择
        logger.info("没有预定义中转站，使用主要枢纽站作为候选")
        return MAJOR_STATIONS[:5]

    def _search_transfer_routes(self, ticket_type, from_loc, to_loc, transfer_stations, date, time=None):
        """查询中转路线"""
        logger.info(f"开始查询中转路线: {from_loc} -> [中转] -> {to_loc}")
        
        all_routes = []
        min_transfer_time = 30  # 最小换乘时间（分钟）
        max_transfer_time = 180  # 最大换乘时间（分钟）
        
        for transfer_station in transfer_stations:
            logger.info(f"查询经由 {transfer_station} 的中转路线")
            
            # 查询第一段: 出发地 -> 中转站
            first_leg = self.get_ticket_info(ticket_type, from_loc, transfer_station, date, time)
            if not first_leg:
                logger.warning(f"未找到从 {from_loc} 到 {transfer_station} 的车次")
                continue
                
            logger.info(f"找到从 {from_loc} 到 {transfer_station} 的车次数量: {len(first_leg)}")
            
            # 查询第二段: 中转站 -> 目的地
            second_leg = self.get_ticket_info(ticket_type, transfer_station, to_loc, date, None)
            if not second_leg:
                logger.warning(f"未找到从 {transfer_station} 到 {to_loc} 的车次")
                continue
                
            logger.info(f"找到从 {transfer_station} 到 {to_loc} 的车次数量: {len(second_leg)}")
            
            # 匹配合适的中转方案
            for train1 in first_leg:
                arrival_time = train1.get('arrivetime', '')
                if not arrival_time:
                    continue
                    
                arrival_time_obj = datetime.strptime(arrival_time, "%H:%M").time()
                arrival_minutes = arrival_time_obj.hour * 60 + arrival_time_obj.minute
                
                for train2 in second_leg:
                    depart_time = train2.get('departtime', '')
                    if not depart_time:
                        continue
                        
                    depart_time_obj = datetime.strptime(depart_time, "%H:%M").time()
                    depart_minutes = depart_time_obj.hour * 60 + depart_time_obj.minute
                    
                    # 计算换乘时间（分钟）
                    # 如果第二段车次时间早于第一段，则认为是第二天的车次
                    transfer_minutes = depart_minutes - arrival_minutes
                    if transfer_minutes < 0:
                        # 跨天情况，加上24小时
                        transfer_minutes += 24 * 60
                        
                    # 判断换乘时间是否合理
                    if min_transfer_time <= transfer_minutes <= max_transfer_time:
                        # 计算总价格（以二等座为例）
                        total_price = self._calculate_total_price(train1, train2)
                        
                        # 计算总时间
                        total_runtime = self._calculate_total_runtime(train1, train2, transfer_minutes)
                        
                        route = {
                            'first_leg': train1,
                            'second_leg': train2,
                            'transfer_station': transfer_station,
                            'transfer_time': transfer_minutes,
                            'total_price': total_price,
                            'total_runtime': total_runtime
                        }
                        all_routes.append(route)
                        logger.info(f"找到可行的中转方案: {train1['trainumber']} -> {train2['trainumber']}, "
                                  f"换乘时间: {transfer_minutes}分钟, 总价格: {total_price}元")
        
        # 按总时间排序
        all_routes.sort(key=lambda x: x['total_runtime'])
        logger.info(f"共找到{len(all_routes)}个可行的中转方案")
        
        # 返回前10个方案
        return all_routes[:10]

    def _calculate_total_price(self, train1, train2):
        """计算两段行程的总价格（默认以二等座为参考）"""
        try:
            # 找出第一段的二等座价格，如果没有则使用一等座或商务座
            price1 = 0
            for seat in train1.get('ticket_info', []):
                if seat.get('seatname') == '二等座':
                    price1 = float(seat.get('seatprice', 0))
                    break
            if price1 == 0:
                # 找不到二等座，尝试其他座位
                for seat in train1.get('ticket_info', []):
                    if seat.get('seatprice'):
                        price1 = float(seat.get('seatprice', 0))
                        break
            
            # 找出第二段的二等座价格
            price2 = 0
            for seat in train2.get('ticket_info', []):
                if seat.get('seatname') == '二等座':
                    price2 = float(seat.get('seatprice', 0))
                    break
            if price2 == 0:
                # 找不到二等座，尝试其他座位
                for seat in train2.get('ticket_info', []):
                    if seat.get('seatprice'):
                        price2 = float(seat.get('seatprice', 0))
                        break
                        
            return price1 + price2
        except Exception as e:
            logger.error(f"计算总价格时出错: {e}")
            return 0

    def _calculate_total_runtime(self, train1, train2, transfer_minutes):
        """计算总行程时间（分钟）"""
        try:
            # 解析第一段运行时间
            runtime1_str = train1.get('runtime', '0小时0分钟')
            runtime1 = self._convert_runtime_to_minutes(runtime1_str)
            
            # 解析第二段运行时间
            runtime2_str = train2.get('runtime', '0小时0分钟')
            runtime2 = self._convert_runtime_to_minutes(runtime2_str)
            
            # 总时间 = 第一段时间 + 换乘时间 + 第二段时间
            total_runtime = runtime1 + transfer_minutes + runtime2
            return total_runtime
        except Exception as e:
            logger.error(f"计算总行程时间时出错: {e}")
            return 0

    def _format_transfer_response(self, routes):
        """格式化中转查询结果"""
        if not routes:
            return "未找到合适的中转方案"
            
        result = ["【中转查询结果】"]
        
        for idx, route in enumerate(routes, 1):
            first_leg = route['first_leg']
            second_leg = route['second_leg']
            transfer_station = route['transfer_station']
            transfer_time = route['transfer_time']
            total_price = route['total_price']
            
            # 计算总时间，格式化为小时和分钟
            total_minutes = route['total_runtime']
            total_hours = total_minutes // 60
            total_mins = total_minutes % 60
            total_time_str = f"{total_hours}小时{total_mins}分钟"
            
            # 拼接结果
            route_info = []
            route_info.append(f"\n{idx}. 【总时长: {total_time_str}】 【总票价: ¥{total_price}】")
            
            # 第一段行程
            route_info.append(f"① {first_leg.get('trainumber')} {first_leg.get('traintype')}: "
                            f"{first_leg.get('departstation')}({first_leg.get('departtime')}) → "
                            f"{transfer_station}({first_leg.get('arrivetime')})")
            
            # 换乘信息
            transfer_hours = transfer_time // 60
            transfer_mins = transfer_time % 60
            route_info.append(f"   🔄 {transfer_station}站内换乘 {transfer_hours}小时{transfer_mins}分钟")
            
            # 第二段行程
            route_info.append(f"② {second_leg.get('trainumber')} {second_leg.get('traintype')}: "
                            f"{transfer_station}({second_leg.get('departtime')}) → "
                            f"{second_leg.get('arrivestation')}({second_leg.get('arrivetime')})")
            
            # 票价信息
            route_info.append("💰票价详情:")
            route_info.append(f"   第一段: " + " | ".join([
                f"{s.get('seatname', '未知')}：¥{s.get('seatprice', '未知')}（余{s.get('seatinventory', 0)}张）"
                for s in first_leg.get('ticket_info', [])[:3]  # 只显示前3种席别
            ]))
            route_info.append(f"   第二段: " + " | ".join([
                f"{s.get('seatname', '未知')}：¥{s.get('seatprice', '未知')}（余{s.get('seatinventory', 0)}张）"
                for s in second_leg.get('ticket_info', [])[:3]  # 只显示前3种席别
            ]))
            
            result.append("\n".join(route_info))
        
        # 添加页脚
        footer = "\n📌提示: 以上为系统推荐的最佳中转方案，按总耗时排序"
        footer += "\n💡如需指定中转站，请使用格式: 中转+经南京+高铁 成都 上海"
        
        return "\n".join(result) + footer

    def _send_error(self, message, e_context):
        """发送错误信息"""
        logger.error(f"错误信息：{message}")
        reply = Reply()
        reply.type = ReplyType.ERROR
        reply.content = message
        e_context["reply"] = reply
        e_context.action = EventAction.BREAK_PASS

    def _format_train_info(self, trains):
        """格式化列车信息"""
        if not trains:
            return "未找到符合条件的车次"
            
        result = []
        for train in trains:
            train_info = [
                f"车次：{train['trainumber']}",
                f"出发：{train['departstation']} {train['departtime']}",
                f"到达：{train['arrivestation']} {train['arrivetime']}",
                f"历时：{train['runtime']}"
            ]
            
            # 添加票价信息
            ticket_info = []
            for ticket in train['ticket_info']:
                status = "✅" if ticket['bookable'] == "有车票" else "❌"
                ticket_info.append(f"{ticket['seatname']}: {status} ¥{ticket['seatprice']}")
            
            train_info.append("票价：" + " | ".join(ticket_info))
            result.append("\n".join(train_info))
            
        return "\n\n".join(result)

    def _process_query(self, e_context: EventContext):
        """处理查询请求"""
        query = e_context["query"]
        logger.debug(f"收到查询请求：{query}")
        
        # 解析查询参数
        ticket_type = "高铁"  # 默认查询高铁
        from_loc = "上海"
        to_loc = "北京"
        time = "12:00"  # 默认中午12点
        
        # 获取车票信息
        trains = self.get_ticket_info(ticket_type, from_loc, to_loc, None, time)
        if trains is None:
            e_context["reply"] = "抱歉，查询失败，请稍后重试"
            e_context.action = EventAction.BREAK_PASS
            return
            
        # 格式化结果
        reply = self._format_train_info(trains)
        e_context["reply"] = reply
        e_context.action = EventAction.BREAK_PASS

    def _convert_runtime_to_minutes(self, runtime_str):
        """将运行时长字符串转换为分钟数"""
        try:
            # 处理格式如 "4小时31分钟"
            hours = 0
            minutes = 0
            hour_match = re.search(r"(\d+)小时", runtime_str)
            if hour_match:
                hours = int(hour_match.group(1))
            minute_match = re.search(r"(\d+)分钟", runtime_str)
            if minute_match:
                minutes = int(minute_match.group(1))
            return hours * 60 + minutes
        except Exception as e:
            logger.error(f"运行时间转换错误：{runtime_str}, {e}")
            return 0
