import os
import time
from decimal import Decimal
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from products.models import Category, Product

BASE_URL = 'https://instrument.ru'
CATALOG_URL = f'{BASE_URL}/catalog/'
FILTER_URL = f'{BASE_URL}/web/catalog/filter/clear/apply/'
USER_AGENT = 'Mozilla/5.0 (compatible; ProtoolCatalogBot/1.0; +https://instrument.ru/catalog/)'
REQUEST_TIMEOUT = 20


def slug_from_url(url):
    path = urlparse(url).path.strip('/')
    parts = path.split('/')
    return parts[-1] if parts else ''


def parse_root_categories(html):
    soup = BeautifulSoup(html, 'lxml')
    nodes = []
    for item in soup.select('div.catalog > div.item'):
        title_a = item.select_one('div.item__title a[href]')
        if not title_a:
            continue
        children = [
            {'name': a.get_text(strip=True), 'url': urljoin(BASE_URL, a['href'])}
            for a in item.select('ul.item__wrapper li a[href]')
        ]
        nodes.append({
            'name': title_a.get_text(strip=True),
            'url': urljoin(BASE_URL, title_a['href']),
            'children': children,
        })
    return nodes


def parse_subcategory_children(html):
    soup = BeautifulSoup(html, 'lxml')
    menu = soup.select_one('div.static-title__menu')
    if not menu:
        return []
    return [
        {'name': a.get_text(strip=True), 'url': urljoin(BASE_URL, a['href'])}
        for a in menu.select('a.item[href]')
    ]


class Command(BaseCommand):
    help = 'Импорт категорий и товаров с instrument.ru/catalog/ (без цены) в модели Category и Product'

    def add_arguments(self, parser):
        parser.add_argument('--category', help='Ограничиться одной корневой категорией по её slug (например avtomobilnyy-instrument)')
        parser.add_argument('--delay', type=float, default=0.4, help='Пауза между запросами к сайту, сек')
        parser.add_argument('--page-size', type=int, default=50, help='Размер страницы товаров при запросе к API')
        parser.add_argument('--max-depth', type=int, default=8, help='Предельная глубина дерева категорий')
        parser.add_argument('--skip-images', action='store_true', help='Не скачивать изображения товаров')

    def handle(self, *args, **options):
        self.delay = options['delay']
        self.page_size = options['page_size']
        self.max_depth = options['max_depth']
        self.skip_images = options['skip_images']
        self.session = self._build_session()

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
            top_category = self._save_category(top['name'], top['url'], parent=None)
            if not top_category:
                continue
            if top['children']:
                for child in top['children']:
                    self._process_category(child['name'], child['url'], top_category, depth=1)
            else:
                self._scrape_products(top_category)

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
        category = self._save_category(name, url, parent)
        if not category:
            return
        if depth >= self.max_depth:
            self._scrape_products(category)
            return

        html = self._get(url)
        if html is None:
            return

        children = parse_subcategory_children(html)
        if not children:
            self._scrape_products(category)
            return

        for child in children:
            self._process_category(child['name'], child['url'], category, depth + 1)

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

    def _scrape_products(self, category):
        page = 1
        while True:
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

        product, created = Product.objects.get_or_create(
            external_id=external_id,
            defaults={'category': category, 'name': name[:255], 'price': Decimal('0')},
        )
        product.category = category
        product.name = name[:255]
        product.description = item.get('description') or ''
        product.product_properties = item.get('properties') or None
        product.save()

        self.products_count += 1
        if self.products_count % 50 == 0:
            self.stdout.write(f'  ...товаров обработано: {self.products_count}')

        if not self.skip_images and (created or not product.image):
            image_url = self._extract_image_url(item)
            if image_url:
                self._download_image(product, image_url)

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
