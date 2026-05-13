# AKShare QDII 数据

> 数据来源: [AKShare 文档](https://akshare.akfamily.xyz)

本文档包含 **AKShare QDII 数据** 分类下的所有 AKShare 接口函数介绍。

---


## T+0 QDII 欧美市场

#### `qdii_e_index_jsl`

**描述**: 集思录-T+0 QDII-欧美市场-欧美指数

**数据源**: https://www.jisilu.cn/data/qdii/#qdiia

**限量说明**: 单次返回所有数据


**输入参数**

| 名称 | 类型 | 描述 |
|------|------|------|
| cookie | str | 需要传入用户登录后的 cookie |


**输出参数**

| 名称 | 类型 | 描述 |
|------|------|------|
| 代码 | object |  |
| 名称 | object |  |
| 现价 | float64 |  |
| 涨幅 | object |  |
| 成交 | float64 | 注意单位: 万元 |
| 场内份额 | int64 | 注意单位: 万份 |
| 场内新增 | int64 | 注意单位: 万份 |
| T-2净值 | float64 |  |
| 净值日期 | object |  |
| T-1估值 | float64 |  |
| 估值日期 | object |  |
| T-1溢价率 | object |  |
| 相关标的 | object |  |
| T-1指数涨幅 | object |  |
| 申购费 | object |  |
| 赎回费 | object |  |
| 托管费 | float64 |  |
| 基金公司 | object |  |

**示例代码**:

```python
import akshare as ak

qdii_e_index_jsl_df = ak.qdii_e_index_jsl()
print(qdii_e_index_jsl_df)
```

---


## 欧美指数

#### `qdii_e_comm_jsl`

**描述**: 集思录-T+0 QDII-欧美市场-欧美商品

**数据源**: https://www.jisilu.cn/data/qdii/#qdiia

**限量说明**: 单次返回所有数据


**输入参数**

| 名称 | 类型 | 描述 |
|------|------|------|
| cookie | str | 需要传入用户登录后的 cookie |


**输出参数**

| 名称 | 类型 | 描述 |
|------|------|------|
| 代码 | object |  |
| 名称 | object |  |
| 现价 | float64 |  |
| 涨幅 | object |  |
| 成交 | float64 | 注意单位: 万元 |
| 场内份额 | int64 | 注意单位: 万份 |
| 场内新增 | int64 | 注意单位: 万份 |
| T-2净值 | float64 |  |
| 净值日期 | object |  |
| T-1估值 | float64 |  |
| 估值日期 | object |  |
| T-1溢价率 | object |  |
| 相关标的 | object |  |
| T-1指数涨幅 | object |  |
| 申购费 | object |  |
| 赎回费 | object |  |
| 托管费 | float64 |  |
| 基金公司 | object |  |

**示例代码**:

```python
import akshare as ak

qdii_e_comm_jsl_df = ak.qdii_e_comm_jsl()
print(qdii_e_comm_jsl_df)
```

---


## T+0 QDII 亚洲市场

#### `qdii_a_index_jsl`

**描述**: 集思录-T+0 QDII-亚洲市场-亚洲指数

**数据源**: https://www.jisilu.cn/data/qdii/#qdiia

**限量说明**: 单次返回所有数据


**输入参数**

| 名称 | 类型 | 描述 |
|------|------|------|
| cookie | str | 需要传入用户登录后的 cookie |


**输出参数**

| 名称 | 类型 | 描述 |
|------|------|------|
| 代码 | object |  |
| 名称 | object |  |
| 现价 | float64 |  |
| 涨幅 | object |  |
| 成交 | float64 | 注意单位: 万元 |
| 场内份额 | int64 | 注意单位: 万份 |
| 场内新增 | int64 | 注意单位: 万份 |
| 净值 | float64 |  |
| 净值日期 | object |  |
| 估值 | float64 |  |
| 溢价率 | object |  |
| 相关标的 | object |  |
| 指数涨幅 | object |  |
| 申购费 | object |  |
| 赎回费 | object |  |
| 托管费 | float64 |  |
| 基金公司 | object |  |

**示例代码**:

```python
import akshare as ak

qdii_a_index_jsl_df = ak.qdii_a_index_jsl()
print(qdii_a_index_jsl_df)
```

---
