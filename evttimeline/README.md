# 事件时间线（evttimeline）

一个独立的 Streamlit 小程序：围绕**某个主题**（行业发展周期、公司经营状态、其它）把关键事件按时间入栈，
支持手工录入、巨潮公告自动采集、检索、脉络整理与 Word 导出。

与 IndustryBoom 的关系：**独立页面、共享数据与能力**。时间线不嵌进行情看板，避免拖慢主页面；
但复用 IndustryBoom 的申万行业目录、成分公司快照与巨潮公告客户端。

## 启动

```powershell
cd C:\kwg\IndustryBoom
.\.venv\Scripts\python.exe -m streamlit run evttimeline/app.py --server.port 8502
```

浏览器打开 http://localhost:8502 ；IndustryBoom 主页面侧边栏顶部有「事件时间线」入口按钮，直达该地址。

## 功能

| 区块 | 说明 |
|---|---|
| 主题 | 新建/切换主题，可关联申万三级行业 ID 与公司代码；删除前建议先下载 JSON 备份 |
| 时间线 | 按年分组展示事件卡片（日期、标题、重要性★、标签、来源链接），每条可就地编辑或删除 |
| 新增事件 | 手工录入日期、标题、正文、标签、重要性、来源链接 |
| 自动采集 | 输入公司代码与时间窗口，从巨潮资讯抓取公告（含临时公告），表格勾选后批量入栈；按「来源链接」与「日期+标题」自动去重 |
| 脉络整理 | 一键生成事件脉络：未配置 LLM 时用规则化模板（按年归纳 + 标签分布 + 关键节点）；配置后由模型输出阶段划分与因果分析 |
| 导出 | Word（.docx）、Markdown、纯文本、JSON 备份；导出范围跟随当前筛选 |

侧边栏检索支持关键词（标题/正文/标签）、标签多选、时间范围与排序。

## 数据存储

`evttimeline/data/topics/<topic_id>.json`，一主题一文件，便于备份与版本管理：

```json
{
  "topic_id": "b049d8f6c231",
  "name": "地产链下行周期",
  "kind": "industry",
  "industry_id": "851811",
  "industry_name": "住宅开发",
  "company_ids": ["000002.SZ"],
  "description": "跟踪政策与销售数据",
  "events": [
    {
      "event_id": "9f2c...",
      "date": "2024-05-17",
      "title": "央行设立保障性住房再贷款",
      "content": "3000 亿元，利率 1.75%",
      "tags": ["政策", "供给"],
      "importance": 5,
      "source": "manual",
      "source_url": ""
    }
  ]
}
```

`source` 取值：`manual`（手工）、`cninfo`（巨潮公告）、`llm`（AI 整理）、`import`（批量导入）。

## 自动采集说明

- 数据源：巨潮资讯公告检索（`cninfo_provider` 复用），只取标题、日期与原文链接，不解析 PDF，速度快
- 默认过滤例行公告：董事会议案、股东大会通知、独立董事意见、月报表、管理办法、更正公告等（`collector.NOISE_PATTERNS`）
- 单次最多 5 家公司、每家最多 6 页（约 180 条），避免长时间占用网络
- 采集结果先预览、再勾选入栈，不会自动写入

## 可选：接入 LLM

脉络整理与事件摘要支持 OpenAI 兼容接口，通过环境变量启用（不配置则走规则化模板，功能不受影响）：

```powershell
$env:EVT_LLM_BASE_URL = "https://api.deepseek.com/v1"
$env:EVT_LLM_API_KEY  = "sk-..."
$env:EVT_LLM_MODEL    = "deepseek-chat"
```

## 为什么是独立页面而不是主页面内的 tab

- 主页面（`app.py`）已承载景气度、行业详情等重数据渲染；时间线交互频繁（表单、编辑、采集），混在一起会互相拖慢
- 数据与状态天然分离：时间线只有 JSON 文件，主页面是行情/财报缓存
- 独立后可以单独重启、单独部署；同时保留入口按钮与共享模块，需要联动时（行业成分、公告采集）随时可加
