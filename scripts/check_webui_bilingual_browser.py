"""Browser smoke checks for localized Assay forms and narrow screens.

Requires a running local Assay API with a registered dataset, Playwright and
Chromium. Uses isolated test inputs; administrator settings are never saved.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from playwright.async_api import async_playwright


async def check(base: str, output: Path) -> dict:
    errors: list[str] = []
    results: dict = {}
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True, args=['--no-sandbox'])
        context = await browser.new_context(viewport={'width': 1440, 'height': 900})
        await context.add_init_script("localStorage.setItem('assay_lang', 'en')")
        page = await context.new_page()
        page.on('pageerror', lambda exc: errors.append(str(exc)))
        toggle = page.locator('#lang-toggle')

        await page.goto(base + '/#/research')
        await page.get_by_label('Dataset', exact=True).locator('option').first.wait_for(state='attached')
        await page.get_by_text('Start a new session', exact=True).click()
        await page.get_by_label('Research goal', exact=True).fill('draft research hypothesis')
        await toggle.click()
        await page.get_by_role('heading', name='研究工作台').wait_for()
        assert await page.get_by_label('研究目标', exact=True).input_value() == 'draft research hypothesis'
        assert await page.locator('details[data-draft=create]').evaluate('(node) => node.open')
        await toggle.click()
        assert await page.get_by_label('Research goal', exact=True).input_value() == 'draft research hypothesis'
        results['research_translation_and_draft'] = True

        await page.goto(base + '/#/factor')
        editor = page.locator('textarea.factor-editor')
        await editor.fill('ts_mean(close,17)')
        await page.get_by_label('Horizons', exact=True).fill('1, 3')
        await toggle.click()
        assert await page.locator('textarea.factor-editor').input_value() == 'ts_mean(close,17)'
        assert await page.get_by_label('持有期', exact=True).input_value() == '1, 3'
        await toggle.click()
        assert await page.locator('textarea.factor-editor').input_value() == 'ts_mean(close,17)'
        results['factor_draft'] = True

        started, release = asyncio.Event(), asyncio.Event()
        async def delayed_factor(route):
            started.set()
            await release.wait()
            await route.fulfill(status=503, json={'detail': 'fixture unavailable'})
        await page.route('**/v1/factor/evaluate', delayed_factor)
        await page.get_by_role('button', name='Evaluate ▶', exact=True).first.click()
        await asyncio.wait_for(started.wait(), 10)
        assert await toggle.is_disabled()
        release.set()
        await page.wait_for_function("!document.querySelector('#lang-toggle').disabled")
        await page.unroute('**/v1/factor/evaluate', delayed_factor)
        await page.route('**/v1/factor/evaluate', lambda route: route.fulfill(
            status=200, content_type='text/event-stream', body='event: eval.started\ndata: {}\n\n'))
        await page.get_by_role('button', name='Evaluate ▶', exact=True).first.click()
        await page.wait_for_function("!document.querySelector('#lang-toggle').disabled")
        await page.get_by_text('Evaluation stream ended before the final report.', exact=False).wait_for()
        await page.unroute('**/v1/factor/evaluate')
        results['inflight_language_lock'] = True
        results['incomplete_stream_fails_visibly'] = True

        for route, selector, value in (
            ('combination', 'textarea.cb-factors', 'ts_mean(close,9)\nts_std(close,7)'),
            ('portfolio', 'textarea', 'ts_mean(close,11)'),
        ):
            await page.goto(base + '/#/' + route)
            field = page.locator(selector).first
            await field.fill(value)
            await toggle.click()
            assert await page.locator(selector).first.input_value() == value
            await toggle.click()
            assert await page.locator(selector).first.input_value() == value
        results['combination_and_portfolio_drafts'] = True

        await page.goto(base + '/#/combination')
        await page.route('**/v1/combination', lambda route: route.fulfill(json={
            'method': 'equal', 'standardize': 'rank', 'horizon': 1,
            'factor_names': ['close'], 'weights': {'close': 1}, 'train': {'ic': 0.12},
        }))
        await page.get_by_role('button', name='Combine ▶').click()
        await page.get_by_text('Chosen method', exact=True).wait_for()
        await toggle.click()
        await page.get_by_text('选用方法', exact=True).wait_for()
        await toggle.click()
        await page.get_by_text('Chosen method', exact=True).wait_for()
        await page.unroute('**/v1/combination')

        await page.goto(base + '/#/portfolio')
        await page.route('**/v1/portfolio/backtest', lambda route: route.fulfill(json={
            'total_return': .1, 'n_trading_days': 2, 'n_rebalances': 1,
            'nav_series': [1., 1.1], 'nav_dates': ['2024-01-02', '2024-01-03'],
            'warnings': [], 'trade_log': [], 'config': {'initial_capital': 100000},
        }))
        await page.get_by_role('button', name='Run backtest ▶').click()
        await page.get_by_text('Performance', exact=True).wait_for()
        await toggle.click()
        await page.get_by_text('绩效', exact=True).wait_for()
        await toggle.click()
        await page.get_by_text('Performance', exact=True).wait_for()
        await page.unroute('**/v1/portfolio/backtest')
        results['completed_results_survive_language_switch'] = True

        await page.goto(base + '/#/data')
        await page.get_by_role('button', name='Keys & Dirs').click()
        await page.get_by_role('button', name='Save config').wait_for()
        secret = page.locator('.dm-field input[type=password]').first
        await secret.fill('temporary-local-test-secret')
        await toggle.click()
        await page.get_by_role('button', name='保存配置').wait_for()
        assert await page.locator('.dm-field input[type=password]').first.input_value() == 'temporary-local-test-secret'
        await toggle.click()
        assert await page.locator('.dm-field input[type=password]').first.input_value() == 'temporary-local-test-secret'
        await page.get_by_role('button', name='Data Setup').click()
        await page.get_by_role('button', name='Save schedule').wait_for()
        start = page.locator('.dm-content input[type=date]').first
        await start.fill('2024-01-02')
        await toggle.click()
        await page.get_by_role('button', name='保存计划').wait_for()
        assert await page.locator('.dm-content input[type=date]').first.input_value() == '2024-01-02'
        results['data_manager_tabs_and_drafts'] = True

        # Two concurrent admin writes must keep language switching locked until both finish.
        job_started, schedule_started = asyncio.Event(), asyncio.Event()
        job_release, schedule_release = asyncio.Event(), asyncio.Event()
        async def delayed_job(route):
            if route.request.method != 'POST':
                await route.continue_()
                return
            job_started.set(); await job_release.wait()
            await route.fulfill(json={'id': 'fixture-job'})
        async def delayed_schedule(route):
            if route.request.method != 'PUT':
                await route.continue_()
                return
            schedule_started.set(); await schedule_release.wait()
            await route.fulfill(json={'markets': []})
        await page.route('**/v1/admin/data/jobs', delayed_job)
        await page.route('**/v1/admin/schedule', delayed_schedule)
        await page.get_by_role('button', name='更新', exact=True).first.click()
        await page.get_by_role('button', name='保存计划').click()
        await asyncio.wait_for(job_started.wait(), 10)
        await asyncio.wait_for(schedule_started.wait(), 10)
        assert await toggle.is_disabled()
        job_release.set()
        await page.wait_for_timeout(150)
        assert await toggle.is_disabled()
        schedule_release.set()
        await page.wait_for_function("!document.querySelector('#lang-toggle').disabled")
        await page.unroute('**/v1/admin/data/jobs', delayed_job)
        await page.unroute('**/v1/admin/schedule', delayed_schedule)
        results['concurrent_admin_language_lock'] = True

        # Login is exercised with isolated auth responses, without sending credentials.
        login = await context.new_page()
        login.on('pageerror', lambda exc: errors.append(str(exc)))
        await login.route('**/auth/status', lambda route: route.fulfill(json={'accounts': True, 'setup_required': False}))
        await login.route('**/auth/me', lambda route: route.fulfill(status=401, json={'detail': 'unauthorized'}))
        await login.goto(base + '/login')
        await login.locator('input[name=username]').fill('test-user')
        await login.locator('input[name=password]').fill('temporary-test-password')
        await login.locator('#lang').click()
        await login.get_by_role('heading', name='登录').wait_for()
        assert await login.locator('input[name=username]').input_value() == 'test-user'
        assert await login.locator('input[name=password]').input_value() == 'temporary-test-password'
        await login.close()
        results['login_draft'] = True
        await context.close()

        widths = {}
        for lang in ('en', 'zh'):
            mobile = await browser.new_context(viewport={'width': 375, 'height': 850}, is_mobile=True, has_touch=True)
            await mobile.add_init_script(f"localStorage.setItem('assay_lang', '{lang}')")
            small = await mobile.new_page()
            small.on('pageerror', lambda exc: errors.append(str(exc)))
            widths[lang] = {}
            for route in ('dashboard', 'research', 'factor', 'library', 'combination', 'portfolio', 'chart', 'data'):
                await small.goto(base + '/#/' + route, wait_until='domcontentloaded')
                await small.wait_for_timeout(450)
                width = await small.evaluate('document.documentElement.scrollWidth')
                widths[lang][route] = width
                assert width <= 375, f'{lang} {route} overflows the mobile viewport: {width}'
            await small.goto(base + '/#/research')
            await small.screenshot(path=str(output / f'research-mobile-{lang}.png'), full_page=True)
            await mobile.close()
        results['mobile_scroll_widths'] = widths
        await browser.close()
    assert not errors, errors
    results['page_errors'] = errors
    return results


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default='http://127.0.0.1:18761')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    result = asyncio.run(check(args.base_url.rstrip('/'), args.output))
    (args.output / 'checks.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps(result, indent=2))
