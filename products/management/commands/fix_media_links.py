import shutil
from urllib.parse import urljoin

from django.core.management.base import BaseCommand
from django.db.models import Q

from products.management.commands.parse_instrument import BASE_URL, FILTER_URL
from products.management.commands.parse_instrument import Command as ParseCommand
from products.models import Product

STALE_PREFIX = 'https://api.protool.kg/media/'


class Command(BaseCommand):
    help = (
        'Точечно заменяет устаревшие ссылки на media (api.protool.kg), оставшиеся '
        'после перехода на прямые ссылки instrument.ru (миграция 0005), свежими '
        'ссылками с instrument.ru — только для тех товаров/фото/сертификатов, '
        'у которых сохранился старый префикс. Остальные поля не трогает.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--delay', type=float, default=0.4, help='Пауза между запросами к сайту, сек')
        parser.add_argument('--page-size', type=int, default=50, help='Размер страницы товаров при запросе к API')
        parser.add_argument('--dry-run', action='store_true', help='Только показать, что будет изменено, не сохранять')

    def handle(self, *args, **options):
        dry_run = options['dry_run']

        parser_cmd = ParseCommand()
        parser_cmd.stdout = self.stdout
        parser_cmd.stderr = self.stderr
        parser_cmd.delay = options['delay']
        parser_cmd.page_size = options['page_size']
        parser_cmd.errors_count = 0
        parser_cmd.session = parser_cmd._build_session()

        can_fetch_details = bool(shutil.which('node'))
        if not can_fetch_details:
            self.stderr.write(self.style.WARNING(
                'Node.js не найден в PATH — сертификаты и галерея фото обновлены не будут, '
                'починятся только ссылки на основное изображение'
            ))

        stale_products = Product.objects.filter(
            Q(image__startswith=STALE_PREFIX)
            | Q(product_certificate__startswith=STALE_PREFIX)
            | Q(images__image__startswith=STALE_PREFIX)
        ).distinct().select_related('category')

        total = stale_products.count()
        self.stdout.write(f'Найдено товаров с устаревшими ссылками: {total}')

        fixed = 0
        not_found = 0
        for product in stale_products:
            item = self._find_item(parser_cmd, product)
            if item is None:
                not_found += 1
                self.stderr.write(self.style.WARNING(
                    f'Не найден в каталоге instrument.ru: {product.article or product.external_id} ({product.name})'
                ))
                continue

            self._refresh_product(parser_cmd, product, item, can_fetch_details, dry_run)
            fixed += 1

        self.stdout.write(self.style.SUCCESS(
            f'Обновлено: {fixed}, не найдено на сайте: {not_found}, ошибок запросов: {parser_cmd.errors_count}'
        ))

    def _find_item(self, parser_cmd, product):
        section_code = product.category.slug if product.category_id else None
        if not section_code:
            return None

        page = 1
        while True:
            data = parser_cmd._get_json(FILTER_URL, params={
                'section_code': section_code,
                'page': page,
                'page_size': parser_cmd.page_size,
            })
            if not data or data.get('status') != 'success':
                return None

            items = (data.get('data') or {}).get('items') or []
            for item in items:
                code = item.get('code') or str(item.get('id') or '')
                if code == product.external_id:
                    return item

            nav = (data.get('data') or {}).get('page_navigation') or {}
            page_count = nav.get('page_count', page)
            if page >= page_count:
                return None
            page += 1

    def _refresh_product(self, parser_cmd, product, item, can_fetch_details, dry_run):
        needs_image = bool(product.image and product.image.startswith(STALE_PREFIX))
        needs_cert = bool(product.product_certificate and product.product_certificate.startswith(STALE_PREFIX))
        needs_gallery = product.images.filter(image__startswith=STALE_PREFIX).exists()

        update_fields = []

        if needs_image:
            image_url = parser_cmd._extract_image_url(item)
            if image_url:
                fresh = urljoin(BASE_URL, image_url)
                self.stdout.write(f'{product}: image -> {fresh}')
                if not dry_run:
                    product.image = fresh
                    update_fields.append('image')

        if (needs_cert or needs_gallery) and can_fetch_details:
            detail = parser_cmd._fetch_detail_data(item.get('url'))
            if detail:
                if needs_gallery:
                    photos = detail.get('photos_main') or []
                    self.stdout.write(f'{product}: gallery -> {len(photos)} фото')
                    if not dry_run:
                        parser_cmd._save_gallery_images(product, photos)

                if needs_cert:
                    for doc in detail.get('documents') or []:
                        filepath = (doc or {}).get('filepath')
                        doc_type = (doc or {}).get('type') or ''
                        if filepath and 'сертификат' in doc_type.lower():
                            fresh_cert = urljoin(BASE_URL, filepath)
                            self.stdout.write(f'{product}: certificate -> {fresh_cert}')
                            if not dry_run:
                                product.product_certificate = fresh_cert
                                update_fields.append('product_certificate')
                            break

        if update_fields and not dry_run:
            product.save(update_fields=update_fields)
