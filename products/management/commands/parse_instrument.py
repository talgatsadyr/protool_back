import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from decimal import Decimal
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from products.models import Category, Product, ProductImage

BASE_URL = 'https://instrument.ru'
CATALOG_URL = f'{BASE_URL}/catalog/'
FILTER_URL = f'{BASE_URL}/web/catalog/filter/clear/apply/'
USER_AGENT = 'Mozilla/5.0 (compatible; ProtoolCatalogBot/1.0; +https://instrument.ru/catalog/)'
REQUEST_TIMEOUT = 20
NUXT_STATE_RE = re.compile(r'window\.__NUXT__=.*?(?=</script>)', re.S)


def slug_from_url(url):
    path = urlparse(url).path.strip('/')
    parts = path.split('/')
    return parts[-1] if parts else ''


def parse_root_categories(html):
    """Корневая страница /catalog/ показывает лишь витрину популярных
    подкатегорий, а не полное дерево — поэтому отсюда берём только
    названия и ссылки верхнеуровневых категорий. Дальше дерево строится
    по SUB_SECTIONS из JSON filter-API (см. Command._process_category)."""
    soup = BeautifulSoup(html, 'lxml')
    nodes = []
    for item in soup.select('div.catalog > div.item'):
        title_a = item.select_one('div.item__title a[href]')
        if not title_a:
            continue
        nodes.append({
            'name': title_a.get_text(strip=True),
            'url': urljoin(BASE_URL, title_a['href']),
        })
    return nodes


class Command(BaseCommand):
    help = 'Импорт категорий и товаров с instrument.ru/catalog/ (без цены) в модели Category и Product'

    def add_arguments(self, parser):
        parser.add_argument('--category', help='Ограничиться одной корневой категорией по её slug (например avtomobilnyy-instrument)')
        parser.add_argument('--delay', type=float, default=0.4, help='Пауза между запросами к сайту, сек')
        parser.add_argument('--page-size', type=int, default=50, help='Размер страницы товаров при запросе к API')
        parser.add_argument('--max-depth', type=int, default=8, help='Предельная глубина дерева категорий')
        parser.add_argument('--skip-images', action='store_true', help='Не скачивать изображения товаров')
        parser.add_argument(
            '--skip-details', action='store_true',
            help='Не заходить на страницу товара за характеристиками, галереей фото и сертификатом',
        )
        parser.add_argument(
            '--skip-existing', action='store_true',
            help='Не трогать товары, уже существующие в БД (по external_id) — только добавлять новые',
        )

    def handle(self, *args, **options):
        self.delay = options['delay']
        self.page_size = options['page_size']
        self.max_depth = options['max_depth']
        self.skip_images = options['skip_images']
        self.skip_details = options['skip_details']
        self.skip_existing = options['skip_existing']
        self.session = self._build_session()

        if not self.skip_details and not shutil.which('node'):
            self.skip_details = True
            self.stderr.write(self.style.WARNING(
                'Node.js не найден в PATH — характеристики, галерея фото и сертификаты не будут спарсены'
            ))

        self.categories_count = 0
        self.products_count = 0
        self.errors_count = 0

        self.stdout.write('Загружаю дерево категорий...')
        root_html = self._get(CATALOG_URL)
        if root_html is None:
            self.stderr.write(self.style.ERROR('Не удалось загрузить страницу каталога'))
            return

        top_nodes = parse_root_categories(root_html)
        if options['category']:
            top_nodes = [n for n in top_nodes if slug_from_url(n['url']) == options['category']]
            if not top_nodes:
                self.stderr.write(self.style.ERROR(f"Категория '{options['category']}' не найдена на странице каталога"))
                return

        for top in top_nodes:
            self._process_category(top['name'], top['url'], parent=None, depth=0)

        self.stdout.write(self.style.SUCCESS(
            f'Готово. Категорий: {self.categories_count}, товаров: {self.products_count}, ошибок: {self.errors_count}'
        ))

    def _build_session(self):
        session = requests.Session()
        session.headers.update({'User-Agent': USER_AGENT})
        retries = Retry(total=3, backoff_factor=0.5, status_forcelist=[429, 500, 502, 503, 504])
        session.mount('https://', HTTPAdapter(max_retries=retries))
        return session

    def _get(self, url, params=None):
        try:
            resp = self.session.get(url, params=params, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            time.sleep(self.delay)
            return resp.text
        except requests.RequestException as exc:
            self.errors_count += 1
            self.stderr.write(self.style.WARNING(f'Ошибка запроса {url}: {exc}'))
            return None

    def _get_json(self, url, params=None):
        try:
            resp = self.session.get(url, params=params, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            time.sleep(self.delay)
            return resp.json()
        except (requests.RequestException, ValueError) as exc:
            self.errors_count += 1
            self.stderr.write(self.style.WARNING(f'Ошибка запроса {url}: {exc}'))
            return None

    def _process_category(self, name, url, parent, depth):
        """Дерево категорий строится по SUB_SECTIONS из ответа filter-API —
        та же ручка, что отдаёт товары, всегда содержит полный и актуальный
        список дочерних разделов (в отличие от HTML-вёрстки каталога, где
        на страницах показана только куцая витрина/устаревшая разметка)."""
        category = self._save_category(name, url, parent)
        if not category:
            return
        if depth >= self.max_depth:
            self._scrape_products(category)
            return

        data = self._get_json(FILTER_URL, params={
            'section_code': category.slug,
            'page': 1,
            'page_size': self.page_size,
        })
        if not data or data.get('status') != 'success':
            return

        sub_sections = ((data.get('data') or {}).get('sections') or {}).get('SUB_SECTIONS') or []
        if not sub_sections:
            self._scrape_products(category, first_page=data)
            return

        for sub in sub_sections:
            child_url = urljoin(BASE_URL, sub.get('SECTION_PAGE_URL') or '')
            self._process_category(sub.get('NAME') or '', child_url, category, depth + 1)

    def _save_category(self, name, url, parent):
        code = slug_from_url(url)
        if not code:
            return None
        category, _ = Category.objects.update_or_create(
            slug=code,
            defaults={'name': name, 'parent': parent},
        )
        self.categories_count += 1
        self.stdout.write(f'Категория: {"  " * (0 if parent is None else 1)}{name} ({code})')
        return category

    def _scrape_products(self, category, first_page=None):
        page = 1
        while True:
            if first_page is not None and page == 1:
                data = first_page
            else:
                data = self._get_json(FILTER_URL, params={
                    'section_code': category.slug,
                    'page': page,
                    'page_size': self.page_size,
                })
            if not data or data.get('status') != 'success':
                break

            items = (data.get('data') or {}).get('items') or []
            if not items:
                break

            for item in items:
                self._save_product(category, item)

            nav = (data.get('data') or {}).get('page_navigation') or {}
            page_count = nav.get('page_count', page)
            if page >= page_count:
                break
            page += 1

    def _save_product(self, category, item):
        external_id = item.get('code') or str(item.get('id') or '')
        name = (item.get('name') or '').strip()
        if not external_id or not name:
            return

        article = item.get('article') or None
        if self.skip_existing and article and Product.objects.filter(article=article).exists():
            return

        product, created = Product.objects.get_or_create(
            external_id=external_id,
            defaults={'category': category, 'name': name[:255], 'price': Decimal('0')},
        )
        product.category = category
        product.name = name[:255]
        product.article = article
        product.description = item.get('description') or ''

        detail = None
        if not self.skip_details:
            detail = self._fetch_detail_data(item.get('url'))
            if detail:
                product.product_properties = self._build_properties(detail.get('properties_blocks')) or None

        product.save()

        self.products_count += 1
        if self.products_count % 50 == 0:
            self.stdout.write(f'  ...товаров обработано: {self.products_count}')

        if not self.skip_images and (created or not product.image):
            image_url = self._extract_image_url(item)
            if image_url:
                self._download_image(product, image_url)

        if detail:
            if not self.skip_images:
                self._save_gallery_images(product, detail.get('photos_main') or [])
            self._save_certificate(product, detail.get('documents') or [])

    def _extract_image_url(self, item):
        preview = item.get('preview_picture')
        if isinstance(preview, dict) and preview.get('src'):
            return preview['src']
        pictures = item.get('pictures') or []
        if pictures and isinstance(pictures[0], dict):
            return pictures[0].get('src')
        return None

    def _download_image(self, product, src):
        url = urljoin(BASE_URL, src)
        try:
            resp = self.session.get(url, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            time.sleep(self.delay)
        except requests.RequestException as exc:
            self.errors_count += 1
            self.stderr.write(self.style.WARNING(f'Не удалось скачать изображение {url}: {exc}'))
            return

        filename = os.path.basename(urlparse(src).path) or f'{product.external_id}.jpg'
        product.image.save(filename, ContentFile(resp.content), save=True)

    def _fetch_detail_data(self, item_url):
        """Страница товара — SSR-Nuxt приложение: список карточек не содержит настоящих
        характеристик/галереи/сертификата, их отдаёт только сама страница товара
        внутри инлайнового скрипта window.__NUXT__."""
        if not item_url or self.skip_details:
            return None

        slug = slug_from_url(item_url)
        if not slug:
            return None

        html = self._get(f'{BASE_URL}/{slug}/')
        if html is None:
            return None

        match = NUXT_STATE_RE.search(html)
        if not match:
            return None

        return self._eval_nuxt_payload(match.group(0))

    def _eval_nuxt_payload(self, js_snippet):
        """window.__NUXT__ — это IIFE с дедуплицированными значениями-переменными
        (классическая сериализация Nuxt 2), поэтому единственный надёжный способ
        получить настоящие значения — выполнить фрагмент в JS-движке (Node.js)."""
        node_script = (
            'global.window = {};'
            + js_snippet + ';'
            + 'var d = (window.__NUXT__.data && window.__NUXT__.data[0] && window.__NUXT__.data[0].data) || {};'
            + 'process.stdout.write(JSON.stringify({'
            + 'properties_blocks: d.properties_blocks || {},'
            + 'photos_main: d.photos_main || [],'
            + 'documents: d.documents || []'
            + '}));'
        )
        # Скрипт может весить сотни КБ (весь __NUXT__ payload) — передаём его через
        # файл, а не аргумент командной строки, иначе упираемся в ARG_MAX.
        with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False, encoding='utf-8') as tmp:
            tmp.write(node_script)
            tmp_path = tmp.name
        try:
            result = subprocess.run(
                ['node', tmp_path],
                capture_output=True, text=True, timeout=15,
            )
        except FileNotFoundError:
            self.skip_details = True
            self.stderr.write(self.style.WARNING(
                'Node.js не найден — характеристики, галерея фото и сертификаты больше не будут парситься'
            ))
            return None
        except subprocess.SubprocessError as exc:
            self.errors_count += 1
            self.stderr.write(self.style.WARNING(f'Ошибка выполнения node: {exc}'))
            return None
        finally:
            os.unlink(tmp_path)

        if result.returncode != 0:
            self.errors_count += 1
            self.stderr.write(self.style.WARNING(f'Ошибка разбора страницы товара: {result.stderr.strip()[:300]}'))
            return None

        try:
            return json.loads(result.stdout)
        except ValueError as exc:
            self.errors_count += 1
            self.stderr.write(self.style.WARNING(f'Не удалось разобрать данные страницы товара: {exc}'))
            return None

    def _build_properties(self, blocks):
        """Каждый блок характеристик (properties_blocks) на сайте отрисован как жирный
        заголовок группы + список пар ключ/значение — делаем его название родительским
        ключом: {"Основные характеристики": {"Длина, м": "25", ...}, ...}."""
        result = {}
        for block in (blocks or {}).values():
            block_name = (block or {}).get('name')
            props = (block or {}).get('properties') or []
            if not block_name or not props:
                continue
            values = {
                prop['name']: prop.get('value')
                for prop in props
                if prop.get('name') and prop.get('value') not in (None, '')
            }
            if values:
                result[block_name] = values
        return result

    def _save_gallery_images(self, product, photos):
        if not photos:
            return

        product.images.all().delete()
        order = 0
        for photo in photos:
            src = (photo or {}).get('standart')
            if not src:
                continue

            url = urljoin(BASE_URL, src)
            try:
                resp = self.session.get(url, timeout=REQUEST_TIMEOUT)
                resp.raise_for_status()
                time.sleep(self.delay)
            except requests.RequestException as exc:
                self.errors_count += 1
                self.stderr.write(self.style.WARNING(f'Не удалось скачать фото {url}: {exc}'))
                continue

            filename = os.path.basename(urlparse(src).path) or f'{product.external_id}_{order}.jpg'
            image = ProductImage(product=product, order=order)
            image.image.save(filename, ContentFile(resp.content), save=True)
            order += 1

    def _save_certificate(self, product, documents):
        for doc in documents:
            filepath = (doc or {}).get('filepath')
            doc_type = (doc or {}).get('type') or ''
            if not filepath or 'сертификат' not in doc_type.lower():
                continue

            url = urljoin(BASE_URL, filepath)
            try:
                resp = self.session.get(url, timeout=REQUEST_TIMEOUT)
                resp.raise_for_status()
                time.sleep(self.delay)
            except requests.RequestException as exc:
                self.errors_count += 1
                self.stderr.write(self.style.WARNING(f'Не удалось скачать сертификат {url}: {exc}'))
                return

            filename = os.path.basename(urlparse(filepath).path) or f'{product.external_id}_certificate.pdf'
            product.product_certificate.save(filename, ContentFile(resp.content), save=True)
            return
