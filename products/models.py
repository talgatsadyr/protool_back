from django.db import models
from mptt.fields import TreeForeignKey
from mptt.models import MPTTModel



class Category(MPTTModel):
    name = models.CharField(max_length=100, verbose_name='Название категории')
    slug = models.SlugField(max_length=255, unique=True, blank=True, null=True, verbose_name='Код категории (источник)')
    description = models.TextField(blank=True, null=True, verbose_name='Описание категории')
    image = models.ImageField(upload_to='category_icons/', blank=True, null=True, verbose_name='Иконка категории')
    parent = TreeForeignKey('self', on_delete=models.CASCADE, blank=True, null=True, related_name='subcategories', verbose_name='Родительская категория')

    def __str__(self):
        return self.name


class Product(models.Model):
    category = models.ForeignKey(Category, on_delete=models.CASCADE, related_name='products')
    external_id = models.SlugField(max_length=255, unique=True, blank=True, null=True, verbose_name='Код продукта (источник)')
    article = models.CharField(max_length=32, blank=True, null=True, db_index=True, verbose_name='Артикул')
    name = models.CharField(max_length=255, verbose_name='Название продукта')
    description = models.TextField(blank=True, null=True, verbose_name='Описание продукта')
    price = models.DecimalField(max_digits=10, decimal_places=2, verbose_name='Цена продукта')
    image = models.ImageField(upload_to='product_images/', blank=True, null=True, verbose_name='Изображение продукта')
    product_properties = models.JSONField(blank=True, null=True, verbose_name='Свойства продукта')
    product_certificate = models.FileField(upload_to='product_certificates/', blank=True, null=True, verbose_name='Сертификат продукта')


    def __str__(self):
        return f"{self.category.name} - {self.name}"


