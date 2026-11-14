from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

from config import cfg
from database import get_setting, set_setting
from pricing_core import calc_price_sync

# Ключи, под которыми цена/маржа хранятся в таблице settings.
# Если в БД ещё ничего не задано — используются значения по умолчанию из .env (cfg).
KEY_STAR_COST = "star_cost_rub"
KEY_MARGIN = "margin_percent"


async def get_price_params(session: AsyncSession) -> tuple[float, float]:
    """Текущие себестоимость за звезду и маржа (%) — из БД, либо дефолт из .env."""
    star_cost = float(await get_setting(session, KEY_STAR_COST, str(cfg.star_cost_rub)))
    margin = float(await get_setting(session, KEY_MARGIN, str(cfg.margin_percent)))
    return star_cost, margin


async def set_star_cost(session: AsyncSession, value: float) -> None:
    await set_setting(session, KEY_STAR_COST, str(value))


async def set_margin(session: AsyncSession, value: float) -> None:
    await set_setting(session, KEY_MARGIN, str(value))


async def calc_price(session: AsyncSession, stars_qty: int) -> tuple[float, float, float]:
    """
    Возвращает (cost_rub, total_rub, profit_rub) — цена берётся из текущих
    настроек (БД, либо дефолт из .env, если в БД ещё не задано).
    """
    star_cost, margin = await get_price_params(session)
    return calc_price_sync(stars_qty, star_cost, margin)
