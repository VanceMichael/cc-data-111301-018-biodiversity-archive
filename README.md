# Darwin Core 资料包的核心记录与扩展关系资料工具

这个 Python 项目用于业务来源资料的检查、原始字节归档与采集查询。核心观察与
关联图片资料各有独立身份：一次观察可以回看多张图片与全部原始行，观察数量
始终只算一次。仅使用 Python 标准库。

## 模块

* `biodiversity_archive/dwca.py`：meta.xml 驱动的通用解析与数据质量校验。
  文件位置、编码、分隔符、引号、忽略行、默认值全部来自 meta.xml；未知术语、
  重复术语、默认值、原始行号都保留；
* `biodiversity_archive/repository.py`：SQLite 采集仓储。数据集 → 多份不可变
  发布 → 核心行/扩展行/问题，支持观察回看、孤立扩展查询与发布对比；
* `biodiversity_archive/core.py`：结构预览（`inspect` 保持原有输出形状，
  `preview` 额外给出行数与问题）；
* `biodiversity_archive/archive.py`：原始 ZIP 的内容寻址归档，文件名不参与身份；
* `fixtures/` 保存本地业务演练资料，`tests/` 检查已有行为。

## 环境

Python 3.11.2。依赖及其间接依赖固定在 `requirements.txt`；空清单表示只使用
标准库。安装只在环境准备阶段执行：

```sh
python3 -m pip install -r requirements.txt
```

## 运行

```sh
# 只读结构预览 / 带行数与问题的预览
python3 -m biodiversity_archive inspect fixtures/archive-1.zip
python3 -m biodiversity_archive preview fixtures/archive-2.zip

# 原始字节归档
python3 -m biodiversity_archive capture fixtures/archive-1.zip --archive .work/raw

# 接收到指定数据集（归档 + 解析 + 校验 + 入库，自动成为当前发布）
python3 -m biodiversity_archive ingest fixtures/archive-1.zip \
    --dataset site-a --archive .work/raw --db .work/catalog.sqlite3

# 查询：数据集目录、历史发布、观察清单、单个观察回看、问题、孤立扩展
python3 -m biodiversity_archive datasets
python3 -m biodiversity_archive publications --dataset site-a
python3 -m biodiversity_archive observations --dataset site-a
python3 -m biodiversity_archive observation --dataset site-a --occurrence-id occ-a
python3 -m biodiversity_archive issues --dataset site-a
python3 -m biodiversity_archive orphans --dataset site-a

# 同一数据集两份发布的差异：区分观察变化与附属图片变化
python3 -m biodiversity_archive diff --dataset site-a
```

归档返回 SHA-256、字节数及原文件名。解析不按固定英文表头读取；core 的 id、
对外 occurrenceID、extension 的 coreid 分栏存储，本地行号只在对应数据集与
发布内有效。图片只登记地址与授权，不下载。输入不可识别时命令失败，不产生
空的成功结果。

## 数据质量与接收语义

缺父记录、重复核心键、重复 occurrenceID、孤立扩展都会作为问题留下并可查询，
不做内连接假装正常。扩展为空不会删除核心观察。合作方交来完整新发布时，只
更新该数据集的当前成员；其他保护地的资料与该数据集的历史发布都保留。
`diff` 用 `is_media_only` 等字段说清变化的是观察本身还是附属资料。详见
`docs/data.md`。

## 开发检查

```sh
python3 -m unittest discover -s tests -v
python3 -m compileall -q biodiversity_archive tests
```

## 本地演示

```sh
python3 -m biodiversity_archive demo
```

演示在临时目录中端到端执行：同一保护地接收三份发布（含一份带孤立扩展、
一份纠正后的资料包），另接收第二家保护地的资料，打印接收凭证、观察回看、
发布差异与数据集目录，自动清理。所有示例身份与业务数值均为虚构，不需要
外部账号，也不发生任何网络访问。
