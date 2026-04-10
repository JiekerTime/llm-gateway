
# Session 管理 & 多维角色卡 — 设计方案

> **状态**: 已实现并部署  
> **创建时间**: 2026-04-11  
> **最后更新**: 2026-04-11

---

## 背景

当前 llm-gateway 是**完全无状态**的：每次 `/chat` 请求，caller 必须把完整的 `messages` 数组（含 system prompt + 全部历史）传过来。对于角色扮演等长对话场景，这导致：

- Token 浪费（角色卡 + 历史每次重传）
- 上下文窗口溢出需 caller 自行截断
- 每个 caller 都要自己实现 session 管理

本方案在网关层引入 **Session 管理** 和 **多维角色卡**，同时保持向后兼容。

---

## 设计原则

1. **向后兼容**：`session_id` 可选，不传仍可保持原来的无状态模式，零迁移成本
2. **轻量存储**：SQLite 持久化，无外部依赖（不引入 Redis）
3. **角色卡多维可组合**：固定维度 + 自定义扩展维度，支持运行时覆盖
4. **角色卡来源灵活**：config.yaml 内联（短卡）+ 独立文件（长卡）
5. **自动截断**：角色卡 system prompt 永远保留，历史按 token 预算截断
6. **角色卡是稳定前缀，不是 RP 引擎**：只负责 deterministic system prompt 组装，不引入 lorebook / 世界书 / 自动剧情状态机
7. **绑定显式化**：session 一旦创建，就绑定 `caller + role_card`

---

## 新增模块

```
llm_gateway/
├── models/
│   ├── role_card.py          ← 多维角色卡数据模型
│   └── session.py            ← Session 状态模型
├── core/
│   ├── role_card_registry.py ← 角色卡加载、组合、查询
│   └── session_manager.py    ← Session CRUD + 自动截断 + TTL
data_runtime/
├── sessions.db               ← SQLite 持久化
data/
└── role_cards/               ← 独立角色卡文件目录
    ├── cat-girl.yaml
    └── coder.yaml
```

---

## 数据模型

### 多维角色卡

```python
# models/role_card.py

class RoleCardDimension(BaseModel):
    """角色卡的单个维度"""
    content: str
    priority: int = 0          # 组装时的排序优先级，越小越靠前

class RoleCard(BaseModel):
    """多维角色卡"""
    name: str                  # 唯一标识
    display_name: str = ""

    # ---- 固定维度 ----
    personality: RoleCardDimension | None = None    # 人设性格
    knowledge: RoleCardDimension | None = None      # 知识背景
    constraints: RoleCardDimension | None = None    # 行为约束
    style: RoleCardDimension | None = None          # 对话风格

    # ---- 自定义扩展维度 ----
    extra_dimensions: dict[str, RoleCardDimension] = {}

    # ---- 角色级配置覆盖 ----
    max_history_turns: int = 50
    max_history_tokens: int = 8000
    temperature: float | None = None   # 覆盖默认温度
    model: str | None = None           # 覆盖默认模型

    def build_system_prompt(self, dimension_overrides: dict[str, str] | None = None) -> str:
        """将多个维度按 priority 排序后拼接成最终 system prompt"""
        ...
```

### Session

```python
# models/session.py

class Session(BaseModel):
    session_id: str
    role_card: str = ""                # 关联的角色卡名称
    caller: str = ""
    messages: list[dict] = []          # 完整历史（不含 system）
    created_at: datetime
    updated_at: datetime
    ttl_hours: int = 24
    dimension_overrides: dict[str, str] = {}   # 运行时覆盖的维度
    metadata: dict = {}                # 自定义扩展字段
    total_tokens_used: int = 0
```

---

## 核心组件

### RoleCardRegistry

```python
# core/role_card_registry.py

class RoleCardRegistry:
    """角色卡注册中心"""

    def __init__(self, config: dict):
        self._cards: dict[str, RoleCard] = {}

    def load_from_config(self, cards_config: dict) -> None:
        """从 config.yaml 的 role_cards.cards 段加载"""

    def load_from_directory(self, card_dir: str) -> None:
        """从 data/role_cards/ 目录加载独立 YAML 文件"""

    def get(self, name: str) -> RoleCard | None:
        """按名称查询角色卡"""

    def list_all(self) -> list[RoleCard]:
        """列出所有角色卡"""
```

### SessionManager

```python
# core/session_manager.py

class SessionManager:
    """Session 生命周期管理"""

    def __init__(self, db_path: str, default_ttl_hours: int = 24):
        self._cache: dict[str, Session] = {}   # 热 session 内存缓存
        self._db_path = db_path                 # SQLite 持久化
        self._lock = asyncio.Lock()

    async def get_or_create(
        self, session_id: str, role_card: str, caller: str
    ) -> Session:
        """获取已有 session 或创建新 session"""

    async def build_full_messages(
        self,
        session: Session,
        new_messages: list[dict],
        role_card: RoleCard,
        dimension_overrides: dict[str, str] | None = None,
    ) -> list[dict]:
        """核心方法：组装完整的 messages 数组
        1. 角色卡 system prompt（含维度覆盖）
        2. 历史消息（自动截断）
        3. 本轮新消息
        """

    async def append_messages(
        self, session_id: str, messages: list[dict], usage_tokens: int = 0
    ) -> None:
        """将本轮的 user + assistant 消息追加到历史"""

    async def delete(self, session_id: str) -> bool:
        """删除 session"""

    async def list_sessions(self, caller: str = "") -> list[Session]:
        """列出活跃 sessions"""

    async def _truncate_history(
        self, messages: list[dict], max_turns: int, max_tokens: int
    ) -> list[dict]:
        """智能截断：保留最近 N 轮，不超过 token 预算"""

    async def _cleanup_expired(self) -> None:
        """后台任务：清理过期 session"""
```

---

## ChatRequest 扩展

```python
class ChatRequest(BaseModel):
    messages: list[dict]
    # ... 现有字段保持不变 ...

    # ---- 新增 Session 相关（全部可选，向后兼容）----
    session_id: str | None = None
    role_card: str | None = None
    dimension_overrides: dict[str, str] = {}
    append_history: bool = True             # 是否自动追加到历史
```

---

## 请求流转（修改 app.py）

```
POST /chat 进入

1. session_id 为空？
   ├─ 是且 role_card 为空 → 原样透传 request.messages 给 backend（完全兼容）
   ├─ 是但 role_card 不为空 → 组装 stateless role-card system prompt，本轮生效但不持久化
   └─ 否 →
       a. SessionManager.get_or_create(session_id, role_card, caller)
       b. RoleCardRegistry.get(role_card) → 获取角色卡
       c. SessionManager.build_full_messages(session, new_messages, role_card, overrides)
          → [system(角色卡)] + [历史(截断)] + [本轮新消息]
       d. full_messages → backend.call()
       e. 响应后 → SessionManager.append_messages(user + assistant)

2. 其余逻辑（fallback、熔断、计量、日志）不变
```

---

## 新增 API 端点

```
# Session 管理
POST   /sessions              创建 session（指定角色卡）
GET    /sessions               列出活跃 sessions
GET    /sessions/{id}          查看 session 信息 + 历史
DELETE /sessions/{id}          删除 session

# 角色卡查询
GET    /role-cards             列出所有角色卡
GET    /role-cards/{name}      查看角色卡详情（含组装后的 system prompt 预览）
```

---

## 配置扩展（config.yaml）

```yaml
# ---- 新增段 ----

sessions:
  enabled: true
  db_path: "data_runtime/sessions.db"
  default_ttl_hours: 24
  cleanup_interval_s: 3600       # 过期清理间隔

role_cards:
  card_dir: "data/role_cards"    # 独立文件目录
  cards:                         # 内联定义（短卡）
    cat-girl:
      display_name: "猫娘"
      personality:
        content: "你是一只可爱的猫娘，说话要带喵~，性格活泼"
        priority: 0
      knowledge:
        content: "你了解各种猫的品种和习性"
        priority: 1
      constraints:
        content: "不要使用粗鲁的语言；始终保持角色"
        priority: 2
      style:
        content: "回复简短可爱，多用颜文字"
        priority: 3
      max_history_turns: 100

    coder:
      display_name: "程序员助手"
      personality:
        content: "你是一个资深全栈工程师"
      knowledge:
        content: "精通 Python, TypeScript, Go"
      constraints:
        content: "代码必须有类型注解和错误处理"
      style:
        content: "先给结论，再给代码，最后解释"
```

---

## 调用示例

```bash
# 1. 创建 session
curl -X POST http://localhost:8525/sessions \
  -H "X-API-Key: <key>" \
  -d '{"role_card": "cat-girl", "caller": "app/chat"}'
# → {"session_id": "sess_abc123", "role_card": "cat-girl", ...}

# 2. 带 session 聊天（只传增量消息）
curl -X POST http://localhost:8525/chat \
  -H "X-API-Key: <key>" \
  -d '{
    "session_id": "sess_abc123",
    "messages": [{"role": "user", "content": "你好呀"}],
    "caller": "app/chat"
  }'

# 3. 运行时覆盖某个维度
curl -X POST http://localhost:8525/chat \
  -H "X-API-Key: <key>" \
  -d '{
    "session_id": "sess_abc123",
    "messages": [{"role": "user", "content": "写个排序算法"}],
    "dimension_overrides": {"knowledge": "你现在精通算法和数据结构"}
  }'

# 4. 无 session 调用（完全向后兼容）
curl -X POST http://localhost:8525/chat \
  -H "X-API-Key: <key>" \
  -d '{"messages": [{"role": "user", "content": "hello"}], "caller": "test"}'

# 5. 查看 session 历史
curl http://localhost:8525/sessions/sess_abc123 \
  -H "X-API-Key: <key>"

# 6. 列出角色卡
curl http://localhost:8525/role-cards \
  -H "X-API-Key: <key>"
```

---

## 落地结果

- [x] **models/role_card.py** — 多维角色卡模型与 deterministic `build_system_prompt()`
- [x] **models/session.py** — Session 状态模型
- [x] **core/role_card_registry.py** — config + 文件目录加载，兼容常见角色卡别名字段
- [x] **core/session_manager.py** — SQLite 持久化、TTL 清理、历史截断、caller/role-card 绑定
- [x] **models/request.py** — `session_id` / `role_card` / `dimension_overrides` / `append_history`
- [x] **app.py** — `/chat` sync + stream 都接入 session / role-card
- [x] **app.py** — `/sessions` 和 `/role-cards` 端点
- [x] **config.yaml** — `sessions` 与 `role_cards` 配置段
- [x] **README.md / wiki / memory** — 当前行为已同步

---

## 实际收敛点

和最初方案相比，当前实现做了几处主动收敛：

1. **新增 stateless role-card 模式**
   - `role_card` 即使不带 `session_id` 也会生效
   - 行为是“注入稳定 system prompt，但不保存历史”
   - 这样可以先用 persona，再决定是否升级为持久 session

2. **session 绑定更严格**
   - 已存在 session 不允许切换 `role_card`
   - 已存在 session 不允许切换 `caller`
   - 避免跨业务误复用同一个 `session_id`

3. **历史只保存非 system 消息**
   - role-card system prompt 每轮重新编译
   - caller 自带的 system message 只影响当轮，不进持久历史

4. **角色卡按“稳定前缀”设计**
   - 当前维度包括：`system_prompt / personality / scenario / knowledge / constraints / style / examples`
   - 支持少量常见别名映射，兼容已有角色卡写法
   - 但不打算把 gateway 做成通用角色扮演平台

5. **运行时状态不进 git**
   - 当前持久化文件位于 `data_runtime/sessions.db`
   - `data_runtime/` 已加入 `.gitignore`

---

## 设计决策记录

| 决策 | 选择 | 理由 |
|---|---|---|
| 存储 | SQLite | 保持轻量、无外部依赖、单机场景够用 |
| 角色卡来源 | config.yaml + 独立文件 | 短卡内联方便，长卡独立文件可维护 |
| 维度模型 | 固定维度 + `extra_dimensions` | 常用维度有类型提示，自定义维度保留扩展性 |
| 向后兼容 | `session_id` 可选 | 不传 = 无状态，零迁移成本 |
| 截断策略 | 保留 system + 最近 N 轮 | 角色卡永远不丢，历史按 token 预算截断 |
| 内存缓存 | 活跃 session 缓存在内存 | 减少 SQLite 读取，参考 TokenStore 模式 |
