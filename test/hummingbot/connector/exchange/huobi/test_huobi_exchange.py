import asyncio
import functools
from decimal import Decimal
from typing import Awaitable, Optional, Callable
from unittest import TestCase
from unittest.mock import AsyncMock

import hummingbot.connector.exchange.huobi.huobi_constants as CONSTANTS

from hummingbot.connector.exchange.huobi.huobi_exchange import HuobiExchange
from hummingbot.core.event.event_logger import EventLogger
from hummingbot.core.event.events import (
    MarketEvent,
    OrderFilledEvent,
    OrderType,
    TradeType,
)


class HuobiExchangeTests(TestCase):
    # the level is required to receive logs from the data source logger
    level = 0

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.base_asset = "COINALPHA"
        cls.quote_asset = "HBOT"
        cls.trading_pair = f"{cls.base_asset}-{cls.quote_asset}"
        cls.symbol = f"{cls.base_asset}{cls.quote_asset}"
        cls.listen_key = "TEST_LISTEN_KEY"

    def setUp(self) -> None:
        super().setUp()

        self.log_records = []
        self.test_task: Optional[asyncio.Task] = None
        self.resume_test_event = asyncio.Event()

        self.exchange = HuobiExchange(
            huobi_api_key="testAPIKey",
            huobi_secret_key="testSecret",
            trading_pairs=[self.trading_pair],
        )

        self.exchange.logger().setLevel(1)
        self.exchange.logger().addHandler(self)

        self._initialize_event_loggers()

    def tearDown(self) -> None:
        self.test_task and self.test_task.cancel()
        super().tearDown()

    def _initialize_event_loggers(self):
        self.buy_order_completed_logger = EventLogger()
        self.sell_order_completed_logger = EventLogger()
        self.order_filled_logger = EventLogger()

        events_and_loggers = [
            (MarketEvent.BuyOrderCompleted, self.buy_order_completed_logger),
            (MarketEvent.SellOrderCompleted, self.sell_order_completed_logger),
            (MarketEvent.OrderFilled, self.order_filled_logger)]

        for event, logger in events_and_loggers:
            self.exchange.add_listener(event, logger)

    def handle(self, record):
        self.log_records.append(record)

    def _is_logged(self, log_level: str, message: str) -> bool:
        return any(record.levelname == log_level and record.getMessage() == message for record in self.log_records)

    def async_run_with_timeout(self, coroutine: Awaitable, timeout: float = 1):
        ret = asyncio.get_event_loop().run_until_complete(asyncio.wait_for(coroutine, timeout))
        return ret

    def _return_calculation_and_set_done_event(self, calculation: Callable, *args, **kwargs):
        if self.resume_test_event.is_set():
            raise asyncio.CancelledError
        self.resume_test_event.set()
        return calculation(*args, **kwargs)

    def test_order_fill_event_takes_fee_from_update_event(self):
        self.exchange.start_tracking_order(
            order_id="OID1",
            exchange_order_id="99998888",
            trading_pair=self.trading_pair,
            order_type=OrderType.LIMIT,
            trade_type=TradeType.BUY,
            price=Decimal("10000"),
            amount=Decimal("1"),
        )

        order = self.exchange.in_flight_orders.get("OID1")

        partial_fill = {
            "eventType": "trade",
            "symbol": "choinalphahbot",
            "orderId": 99998888,
            "tradePrice": "10050.0",
            "tradeVolume": "0.1",
            "orderSide": "buy",
            "aggressor": True,
            "tradeId": 1,
            "tradeTime": 998787897878,
            "transactFee": "10.00",
            "feeDeduct ": "0",
            "feeDeductType": "",
            "feeCurrency": "usdt",
            "accountId": 9912791,
            "source": "spot-api",
            "orderPrice": "10000",
            "orderSize": "1",
            "clientOrderId": "OID1",
            "orderCreateTime": 998787897878,
            "orderStatus": "partial-filled"
        }

        message = {
            "ch": CONSTANTS.HUOBI_TRADE_DETAILS_TOPIC,
            "data": partial_fill
        }

        mock_user_stream = AsyncMock()
        mock_user_stream.get.side_effect = functools.partial(self._return_calculation_and_set_done_event,
                                                             lambda: message)

        self.exchange.user_stream_tracker._user_stream = mock_user_stream

        self.test_task = asyncio.get_event_loop().create_task(self.exchange._user_stream_event_listener())
        self.async_run_with_timeout(self.resume_test_event.wait())

        self.assertEqual(Decimal("10"), order.fee_paid)
        self.assertEqual(1, len(self.order_filled_logger.event_log))
        fill_event: OrderFilledEvent = self.order_filled_logger.event_log[0]
        self.assertEqual(0.0, fill_event.trade_fee.percent)
        self.assertEqual([(partial_fill["feeCurrency"].upper(), Decimal(partial_fill["transactFee"]))],
                         fill_event.trade_fee.flat_fees)
        self.assertTrue(self._is_logged(
            "INFO",
            f"Filled {Decimal(partial_fill['tradeVolume'])} out of {order.amount} of order "
            f"{order.order_type.name}-{order.client_order_id}"
        ))

        self.assertEqual(0, len(self.buy_order_completed_logger.event_log))

        complete_fill = {
            "eventType": "trade",
            "symbol": "choinalphahbot",
            "orderId": 99998888,
            "tradePrice": "10060.0",
            "tradeVolume": "0.9",
            "orderSide": "buy",
            "aggressor": True,
            "tradeId": 2,
            "tradeTime": 998787897878,
            "transactFee": "30.0",
            "feeDeduct ": "0",
            "feeDeductType": "",
            "feeCurrency": "usdt",
            "accountId": 9912791,
            "source": "spot-api",
            "orderPrice": "10000",
            "orderSize": "1",
            "clientOrderId": "OID1",
            "orderCreateTime": 998787897878,
            "orderStatus": "partial-filled"
        }

        message["data"] = complete_fill

        self.resume_test_event = asyncio.Event()
        mock_user_stream = AsyncMock()
        mock_user_stream.get.side_effect = functools.partial(self._return_calculation_and_set_done_event,
                                                             lambda: message)

        self.exchange.user_stream_tracker._user_stream = mock_user_stream

        self.test_task = asyncio.get_event_loop().create_task(self.exchange._user_stream_event_listener())
        self.async_run_with_timeout(self.resume_test_event.wait())

        self.assertEqual(Decimal("40"), order.fee_paid)

        self.assertEqual(2, len(self.order_filled_logger.event_log))
        fill_event: OrderFilledEvent = self.order_filled_logger.event_log[1]
        self.assertEqual(0.0, fill_event.trade_fee.percent)
        self.assertEqual([(complete_fill["feeCurrency"].upper(), Decimal(complete_fill["transactFee"]))],
                         fill_event.trade_fee.flat_fees)

        # The order should be marked as complete only when the "done" event arrives, not with the fill event
        self.assertFalse(self._is_logged(
            "INFO",
            f"The LIMIT_BUY order {order.client_order_id} has completed according to order delta websocket API."
        ))

        self.assertEqual(0, len(self.buy_order_completed_logger.event_log))
