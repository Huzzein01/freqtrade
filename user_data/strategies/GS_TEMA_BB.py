# pragma pylint: disable=missing-docstring, invalid-name
"""GS_TEMA_BB — TEMA + RSI + Bollinger Band mean-reversion strategy.

Ported from freqtrade's sample_strategy.py and tuned for the instruments
GS already trades (ETH, SOL, LTC). Designed to run in dry-run alongside
the GS Alpaca agent and share training data via Supabase.

Entry (long):
  - RSI crossed above 30  (oversold recovery)
  - TEMA <= BB middle      (price below equilibrium)
  - TEMA rising            (momentum confirming)
  - Volume > 0

Exit:
  - RSI crossed above 70  (overbought)
  - TEMA > BB middle
  - TEMA falling
"""

from pandas import DataFrame
from functools import reduce
import talib.abstract as ta
from technical import qtpylib
from freqtrade.strategy import IStrategy, IntParameter


class GS_TEMA_BB(IStrategy):
    INTERFACE_VERSION = 3
    can_short = False

    # --- Risk parameters (conservative — matches GS paper-trading phase) ---
    minimal_roi = {
        "120": 0.0,   # break-even after 2h
        "60":  0.01,
        "30":  0.02,
        "0":   0.04,
    }
    stoploss = -0.05          # 5% hard stop — every trade carries a stop
    trailing_stop = True
    trailing_stop_positive = 0.01
    trailing_stop_positive_offset = 0.02
    trailing_only_offset_is_reached = True

    timeframe = "1h"          # matches GS's 1H bar resolution
    process_only_new_candles = True
    use_exit_signal = True
    exit_profit_only = False
    startup_candle_count: int = 50

    order_types = {
        "entry":   "limit",
        "exit":    "limit",
        "stoploss": "market",
        "stoploss_on_exchange": True,
    }
    order_time_in_force = {"entry": "GTC", "exit": "GTC"}

    # --- Hyperoptable parameters ---
    buy_rsi      = IntParameter(20, 40, default=30, space="buy",  optimize=True)
    sell_rsi     = IntParameter(60, 80, default=70, space="sell", optimize=True)
    tema_period  = IntParameter(7,  21, default=9,  space="buy",  optimize=True)

    plot_config = {
        "main_plot": {
            "tema": {},
            "bb_middleband": {"color": "gray"},
            "bb_upperband":  {"color": "green"},
            "bb_lowerband":  {"color": "red"},
        },
        "subplots": {
            "RSI": {"rsi": {"color": "orange"}},
        },
    }

    # ── Indicators ────────────────────────────────────────────────────────────

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # RSI
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)

        # TEMA
        dataframe["tema"] = ta.TEMA(dataframe, timeperiod=self.tema_period.value)

        # Bollinger Bands (20-period, 2 std)
        bollinger = qtpylib.bollinger_bands(
            qtpylib.typical_price(dataframe), window=20, stds=2
        )
        dataframe["bb_lowerband"]  = bollinger["lower"]
        dataframe["bb_middleband"] = bollinger["mid"]
        dataframe["bb_upperband"]  = bollinger["upper"]
        dataframe["bb_percent"]    = (
            (dataframe["close"] - dataframe["bb_lowerband"])
            / (dataframe["bb_upperband"] - dataframe["bb_lowerband"])
        )

        # ADX — regime filter (only trade when trending or recovering)
        dataframe["adx"] = ta.ADX(dataframe)

        # ATR — for position sizing reference
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)

        return dataframe

    # ── Entry ─────────────────────────────────────────────────────────────────

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        conditions = [
            qtpylib.crossed_above(dataframe["rsi"], self.buy_rsi.value),
            dataframe["tema"] <= dataframe["bb_middleband"],
            dataframe["tema"] > dataframe["tema"].shift(1),
            dataframe["volume"] > 0,
        ]
        dataframe.loc[reduce(lambda x, y: x & y, conditions), "enter_long"] = 1
        return dataframe

    # ── Exit ──────────────────────────────────────────────────────────────────

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        conditions = [
            qtpylib.crossed_above(dataframe["rsi"], self.sell_rsi.value),
            dataframe["tema"] > dataframe["bb_middleband"],
            dataframe["tema"] < dataframe["tema"].shift(1),
            dataframe["volume"] > 0,
        ]
        dataframe.loc[reduce(lambda x, y: x & y, conditions), "exit_long"] = 1
        return dataframe
