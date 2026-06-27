"""
endpoints.py —— 接口配置。✅ 全部来自前端源码 index-DBz8ZwYy.js，已确认，无需再猜。

架构要点：
- 农场自身动作（收获/种植/铲除/解锁/用道具/卖果）走 WebSocket。
- 好友互动 + 任务/邮件 + 自家除草除虫 走 REST。
- 好友用 player_key 标识，传参字段名为 target_key。
- 放草 mark_type="weeds"，放虫 mark_type="worm"；除草除虫 clean_type 同此取值。
"""

# =========================================================================
# REST 路径（base = config.BASE_API_URL，鉴权 Authorization: Bearer <JWT>）
# {x} 占位会被代码替换
# =========================================================================
ENDPOINTS = {
    "login":          "/api/auth/login",                 # POST {"init_data": "..."}
    "profile":        "/api/game/profile",               # GET  角色信息
    "config":         "/api/config",                     # GET
    "levels":         "/api/levels",                     # GET
    "welcome_back":   "/api/game/welcome-back-summary",  # GET

    # 任务
    "tasks":          "/api/game/tasks",                 # GET (可选 ?category=)
    "claim_task":     "/api/game/tasks/{task_code}/claim",  # POST

    # 邮件
    "mails":          "/api/game/mails",                 # GET
    "read_mail":      "/api/game/mails/{mail_id}/read",  # POST
    "claim_mail":     "/api/game/mails/{mail_id}/claim", # POST

    # 好友
    "friends":        "/api/game/friends",               # GET 好友列表(含 player_key)
    "visitors":       "/api/game/visitors",              # GET 访客列表
    "visit":          "/api/game/visit",                 # POST {"target_key": player_key}
    "steal":          "/api/game/steal-crops",           # POST {"target_key": player_key}

    # 好友农场标记（放草放虫）/ 清理（在好友农场除草除虫）/ 用道具
    "friend_mark":    "/api/game/friend-farm/mark",      # POST {target_key, plot_index, mark_type}
    "friend_clean":   "/api/game/friend-farm/clean",     # POST {target_key, clean_type}
    "friend_use_tool":"/api/game/friend-farm/use-tool",  # POST {target_key, plot_index, tool_id}

    # 自家农场除草除虫（清理别人放在你地里的草/虫 -> 今日除草/除虫任务）
    "clean_marks":    "/api/game/clean-marks",           # POST {"clean_type": "weeds"|"worm"}
}

# 标记/清理类型取值（源码确认）
MARK_WEED = "weeds"   # 草
MARK_PEST = "worm"    # 虫

# 好友列表分页参数（源码：page / page_size）
FRIENDS_PAGE_PARAM = "page"
FRIENDS_PAGESIZE_PARAM = "page_size"
FRIENDS_PAGE_SIZE = 10

# =========================================================================
# WebSocket 动作（wss://example.com/api/game/ws；先发 {"type":"auth","token":JWT}）
# 发送格式：{"type":"action","rid":N,"action":"<名>","data":{...}}
# 下面是源码确认的动作名（值即真实 action 名）
# =========================================================================
WS_ACTIONS = {
    "harvest":      "harvest",       # data {"plot_index": N} 收获单块
    "harvest_all":  "harvest_all",   # data {}               一次收获全部成熟
    "plant":        "plant",         # data {"plot_index": N, "crop_id": "..."}
    "clear_plot":   "clear_plot",    # data {"plot_index": N} 铲除作物
    "unlock_plot":  "unlock_plot",   # data {}               解锁下一块地
    "upgrade_plot": "upgrade_plot",  # data {"plot_index": N}
    "use_tool":     "use_tool",      # data {"tool_id": "...", "plot_index": N}
    "sell_fruits":  "sell_fruits",   # data {"crop_id": "...", "count": N}
    "get_inventory":      "get_inventory",        # 种子背包
    "get_fruit_inventory":"get_fruit_inventory",  # 果实背包
    "get_tool_inventory": "get_tool_inventory",
    "get_plots":    "get_plots",     # 主动拉地块（连上时也会被动推 type=plots）
}

# 需要带人机通行证(data.human_pass)的 WS 动作（逆向前端 index-*.js：_m=Set[plant,harvest,
# harvest_all]）。这些动作触发风控，过 human-verify 后每次发送都要带通行证；其余只读/轻动作不带。
WS_PASS_ACTIONS = frozenset({"harvest", "harvest_all", "plant"})

# WS 协议字段（已确认，勿改）
WS_FIELD_TYPE = "type"
WS_TYPE_ACTION = "action"
WS_TYPE_RESULT = "result"
WS_FIELD_RID = "rid"
WS_FIELD_ACTION = "action"
WS_FIELD_DATA = "data"
WS_FIELD_OK = "ok"
WS_FIELD_DATA_RESULT = "data"
