import logging
from typing import Protocol

from models.tool import ToolResult
from tools.base import BusinessTool

logger = logging.getLogger(__name__)


class InventoryService(Protocol):
    async def check_inventory(self, product_id: str, size: str | None = None) -> dict: ...


class ProductService(Protocol):
    async def get_product(self, product_id: str) -> dict: ...


class ShippingService(Protocol):
    async def get_shipping(self, destination: str | None = None) -> dict: ...


class OrderService(Protocol):
    async def get_order(self, order_id: str, customer_id: str) -> dict: ...


class CustomerService(Protocol):
    async def get_customer(self, customer_id: str) -> dict: ...


class ConversationHistoryService(Protocol):
    async def get_conversation_history(self, conversation_id: str, limit: int = 50) -> dict: ...


class ServiceMethodTool(BusinessTool):
    """DI adapter around a real service method; it never supplies fake data."""

    def __init__(self, name: str, service, method_name: str):
        self.name = name
        self.service = service
        self.method_name = method_name

    async def execute(self, arguments: dict) -> ToolResult:
        method = getattr(self.service, self.method_name)
        try:
            data = dict((await method(**arguments)) or {})
            if not data:
                return ToolResult(
                    tool_name=self.name,
                    success=False,
                    error="business service returned no verified data",
                )
            return ToolResult(tool_name=self.name, success=True, data=data)
        except Exception as exc:
            logger.warning("[tools] business tool %s failed", self.name, exc_info=True)
            return ToolResult(
                tool_name=self.name,
                success=False,
                error=f"{type(exc).__name__}: business service request failed",
            )


def register_business_services(registry, *, inventory=None, product=None, shipping=None,
                               order=None, customer=None, conversation_history=None):
    mappings = (
        ("check_inventory", inventory, "check_inventory"),
        ("get_product", product, "get_product"),
        ("get_shipping", shipping, "get_shipping"),
        ("get_order", order, "get_order"),
        ("get_customer", customer, "get_customer"),
        (
            "get_conversation_history", conversation_history,
            "get_conversation_history",
        ),
    )
    for name, service, method in mappings:
        if service is not None:
            registry.register(ServiceMethodTool(name, service, method))
