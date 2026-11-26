# 淘汰赛改判 API（Tournament Reversal API）

裁判更正一场胜负前，先预演新胜负对后续对阵的影响、列出需要作废的已赛成绩，
确认无误后再原子提交。FastAPI + SQLite，Docker Compose 一键启动。

## 业务规则

- 一份签表最多 **32 场**，按 `match_id = 1..N` 定义。
- 每个席位（slot）有三种来源：
  - 固定队伍：`{"type": "team", "team": "A"}`
  - 前场胜队：`{"type": "winner", "match_id": 1}`
  - 前场负队：`{"type": "loser", "match_id": 1}`
  - 引用只能指向**更早**的场次（天然无环）。
- **对阵确定后才可录分**：席位依赖的前场没有有效成绩时，该席位为 `tbd`，
  录分返回 `409`。
- 改判分两步：
  1. `.../corrections/preview` 预演，返回 `preview_token`、改判后对阵投影、
     **待作废成绩清单**和保留清单；
  2. `.../corrections/commit` 凭 token 提交，改判与作废在**同一事务**生效。
- 作废判定（对目标场次之后的每场比赛按顺序推演）：
  - 预演后两队**仍是原来的两队**（集合相同）→ 成绩**保留**，胜队按队伍身份
    重新对位（席位可能互换）；
  - 任一席位 `tbd` 或队伍发生变化 → 成绩**作废**，且作废后停止向后续派队，
    可能级联产生更多 `tbd`。
- **重审保护**：提交时重新计算指纹，若目标场原胜队或待作废清单（含各场记录
  的胜/负/版本）相比预演时发生变化，返回 `409 stale`，需重新预演。
- **无关录分**：不触及目标场胜队、也不触及待作废清单的录分（例如改个比分）
  不会使预览失效，仍可按原预览提交。
- 所有成绩只追加版本、不物理删除；作废 = `active=0`，历史可审计。每场比赛
  至多一条 `active=1` 记录。

## 启动（Docker Compose）

```bash
docker compose up --build
# 服务: http://localhost:8000  文档: http://localhost:8000/docs
# SQLite 持久化在 compose volume bracket-data (/data/bracket.db)
```

## 本地无 Docker 时运行

```bash
pip install -r requirements.txt
export BRACKET_DB=/tmp/bracket.db
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查 |
| POST | `/tournaments` | 创建签表（1–32 场） |
| GET | `/tournaments/{id}` | 查看签表、席位解析、有效成绩 |
| POST | `/tournaments/{id}/matches/{mid}/score` | 录分（须对阵已确定） |
| POST | `/tournaments/{id}/corrections/preview` | 改判预演，返回 token 与作废清单 |
| POST | `/tournaments/{id}/corrections/commit` | 凭 token 原子提交 |

### 演示

```bash
python scripts/demo.py            # 默认 http://127.0.0.1:8000
python scripts/demo.py http://host:port
```

### 自测

```bash
python tests/test_api.py
```

## 目录

```
app/            FastAPI 应用（models/db/engine/main）
tests/          关键流程自测（9 个用例）
scripts/demo.py 端到端 API 演示
Dockerfile / docker-compose.yml / requirements.txt
```
