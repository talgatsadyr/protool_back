from rest_framework import serializers

from .models import Category, Product, ProductImage


class CategorySerializer(serializers.ModelSerializer):
    children = serializers.SerializerMethodField()

    class Meta:
        model = Category
        fields = ('id', 'name', 'slug', 'description', 'image', 'children')

    def get_children(self, obj):
        return CategorySerializer(obj.get_children(), many=True, context=self.context).data


class CategoryShortSerializer(serializers.ModelSerializer):
    class Meta:
        model = Category
        fields = ('id', 'name', 'slug')


class ProductListSerializer(serializers.ModelSerializer):
    category = CategoryShortSerializer(read_only=True)

    class Meta:
        model = Product
        fields = ('id', 'external_id', 'name', 'price', 'image', 'category', 'article')


class ProductImageSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProductImage
        fields = ('id', 'image', 'order')


class ProductDetailSerializer(serializers.ModelSerializer):
    category = CategoryShortSerializer(read_only=True)
    images = ProductImageSerializer(many=True, read_only=True)
    images_count = serializers.IntegerField(source='images.count', read_only=True)

    class Meta:
        model = Product
        fields = (
            'id', 'external_id', 'name', 'description', 'price', 'image',
            'product_properties', 'product_certificate', 'category', 'article',
            'images', 'images_count',
        )
