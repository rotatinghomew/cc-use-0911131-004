"""重述分析（restatement analysis）子包。

职责沿用既有流水线分层：

* feeds          -> :mod:`market_index_bureau.restatement.feed`
* normalization  -> :mod:`market_index_bureau.restatement.normalize` 与
                    :mod:`market_index_bureau.restatement.robust`
* weights        -> :mod:`market_index_bureau.restatement.panel` 与
                    :mod:`market_index_bureau.restatement.weights`
* indices        -> :mod:`market_index_bureau.restatement.engine`
* vintages       -> :mod:`market_index_bureau.restatement.vintages` 与
                    :mod:`market_index_bureau.restatement.store`
"""

__all__ = []
