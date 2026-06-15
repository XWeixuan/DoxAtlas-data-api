from __future__ import annotations

from typing import Any, Iterable


def _fmt(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)


def _row(values: Iterable[Any], widths: list[int]) -> str:
    return " | ".join(_fmt(value).ljust(width) for value, width in zip(values, widths))


def _table(headers: list[str], rows: list[list[Any]]) -> str:
    widths = [len(header) for header in headers]
    for row in rows:
        for index, value in enumerate(row):
            widths[index] = max(widths[index], len(_fmt(value)))
    line = "-+-".join("-" * width for width in widths)
    rendered = [_row(headers, widths), line]
    rendered.extend(_row(row, widths) for row in rows)
    return "\n".join(rendered)


def format_media_crawl_summary(summary: dict[str, Any]) -> str:
    channels = sorted(
        set(summary.get("list_by_channel", {}))
        | set(summary.get("final_by_channel", {}))
        | set(summary.get("detail_success_by_channel", {}))
        | set(summary.get("detail_failed_by_channel", {}))
    )
    rows = []
    for channel in channels:
        rows.append(
            [
                channel,
                summary.get("list_by_channel", {}).get(channel, 0),
                summary.get("final_by_channel", {}).get(channel, 0),
                summary.get("detail_success_by_channel", {}).get(channel, 0),
                summary.get("detail_failed_by_channel", {}).get(channel, 0),
                summary.get("list_failures_by_channel", {}).get(channel, 0),
                summary.get("list_proxy_ips_by_channel", {}).get(channel, 0),
                round(float(summary.get("list_seconds_by_channel", {}).get(channel, 0) or 0), 2),
            ]
        )
    return (
        "\n[CN/HK Media Crawl Summary]\n"
        f"market={summary.get('market')} ticker={summary.get('ticker')} terms={', '.join(summary.get('terms') or [])}\n"
        f"records={summary.get('records')} list_items={summary.get('list_items')} "
        f"list={summary.get('list_seconds')}s detail={summary.get('detail_seconds')}s total={summary.get('total_seconds')}s\n"
        f"detail_selected={summary.get('detail_selected', 0)} detail_success={summary.get('detail_success', 0)} "
        f"detail_failed={summary.get('detail_failed', 0)} success_rate={summary.get('detail_success_rate', 0)} "
        f"detail_proxy_ips={summary.get('detail_proxy_ips', 0)} garbled_dropped={summary.get('garbled_dropped', 0)} "
        f"length_filtered={summary.get('length_filtered', 0)} body_success_rate={summary.get('body_success_rate', 0)}\n"
        + _table(
            ["channel", "list", "final", "detail_ok", "detail_fail", "list_fail", "proxy_ip", "list_s"],
            rows,
        )
    )


def format_social_crawl_summary(summary: dict[str, Any]) -> str:
    return (
        "\n[CN/HK Social Crawl Summary]\n"
        f"ticker={summary.get('ticker')} channel={summary.get('channel')}\n"
        f"records={summary.get('records')} list_items={summary.get('list_items')} "
        f"list={summary.get('list_seconds')}s detail={summary.get('detail_seconds')}s total={summary.get('total_seconds')}s\n"
        f"detail_success={summary.get('detail_success', 0)} detail_failed={summary.get('detail_failed', 0)} "
        f"success_rate={summary.get('detail_success_rate', 0)} timed_out={summary.get('timed_out', False)} "
        f"list_proxy_ips={summary.get('list_proxy_ips', 0)} detail_proxy_ips={summary.get('detail_proxy_ips', 0)} "
        f"length_filtered={summary.get('length_filtered', 0)} body_success_rate={summary.get('body_success_rate', 0)}"
    )
