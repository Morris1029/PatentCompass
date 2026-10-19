# Patent Navigation UI Draft Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 基于专利导航竞品调研，制作可在本地浏览和跳转的简洁 HTML 设计草稿 V0.4。

**Architecture:** 沿用现有静态多页原型和共享 CSS。统一范围栏组织技术、竞争、地域三个分析视角，按需展开分类与证据，候选方向与报告各保留独立页面。当前原型保存在 prototype-v3。

**Tech Stack:** HTML、CSS、原生 details 与本地链接；无 JavaScript、后端或外部资源。

---

## 设计取舍

采用分析工作台作为主要结果入口，将详细方法折叠。综合仪表盘会增加同屏组件，纯报告页面则不便比较分支；新版每个视角突出一张主图、简短判断与相关专利。

参考大为的专题技术树、智慧芽的报告章节组织、Orbit 的图表与证据联动。保留本项目范围确认、候选补检、反例、判断修正、报告追问与局部更新的主线。

## Task 1: 统一视觉和入口

- 修改 prototype/assets/minimal.css、index.html、scope.html。
- 输入 → 确认范围 → 分析；保留通用领域，不固定行业。
- 验证输入标签、键盘焦点、原生折叠和本地链接。

## Task 2: 分析工作台

- 修改 prototype/review.html、landscape.html；新增 competition.html、geography.html。
- 同一范围栏，技术/竞争/地域三个页面入口。
- 技术树按需展开，选择分支进入本地分支示例；相关记录可打开证据页。
- 只绘制分类或分析结构示意，不虚构专利统计、企业或编号。
- 地域口径分开保留申请人所在地、申请布局地和有效保护地。

## Task 3: 候选方向与证据

- 修改 prototype/opportunity.html、evidence.html。
- 候选记录关联支持依据、反例、补检、修正判断和下一步验证。
- 证据记录待导入；统计数据集与代表记录分开。
- 点击记录打开详情，默认减少同屏内容。

## Task 4: 报告与后续分析

- 修改 prototype/report.html、question.html；同步 report-print.html 的导航与版本说明。
- 左侧章节、正文和按需追问；保留完整报告十章及附录。
- 提示局部更新涉及的章节和数据版本；当前只演示页面跳转。
- 更新 prototype/README.md。

## 最小验证

1. 检查全部本地 href、表单 action、CSS 与页内锚点。
2. 在 1440px 和 390px 视口检查核心页面，无页面横向溢出。
3. 实际点击技术分支、分析视角、候选证据和报告追问；检查默认折叠和详情展开。
4. 核对所有示意图、未导入记录和未执行补检的标注；不混入旧版权审查业务。

本次不提交 Git、不连接数据源、不执行专利分析；用户已授权直接制作可评审草稿。
