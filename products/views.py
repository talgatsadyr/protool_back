from django.db.models import DecimalField, ExpressionWrapper, F
from django.db.models.functions import Abs
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from .models import Category, Product
from .serializers import CategorySerializer, ProductDetailSerializer, ProductListSerializer

SIMILAR_PRODUCTS_DEFAULT_LIMIT = 10
SIMILAR_PRODUCTS_MAX_LIMIT = 50

PRODUCT_ORDERING = {
    'cheap': ('price',),
    'expensive': ('-price',),
    'name': ('name',),
}


def apply_product_ordering(queryset, ordering_param):
    ordering = PRODUCT_ORDERING.get(ordering_param)
    if ordering:
        return queryset.order_by(*ordering)
    return queryset


class ProductPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = 'page_size'
    max_page_size = 100


class CategoryViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = CategorySerializer
    permission_classes = [AllowAny]

    def get_queryset(self):
        if self.action == 'list':
            return Category.objects.filter(parent__isnull=True)
        return Category.objects.all()

    @action(detail=True, methods=['get'])
    def products(self, request, pk=None):
        category = self.get_object()
        category_ids = [category.pk] + list(category.get_descendants().values_list('pk', flat=True))
        queryset = Product.objects.filter(category_id__in=category_ids).select_related('category')
        queryset = apply_product_ordering(queryset, request.query_params.get('ordering'))

        paginator = ProductPagination()
        page = paginator.paginate_queryset(queryset, request)
        serializer = ProductListSerializer(page, many=True, context={'request': request})
        return paginator.get_paginated_response(serializer.data)


class ProductViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Product.objects.select_related('category').prefetch_related('images').exclude(price__isnull=True).exclude(price__lte=0)
    permission_classes = [AllowAny]
    pagination_class = ProductPagination

    def get_serializer_class(self):
        if self.action == 'list':
            return ProductListSerializer
        return ProductDetailSerializer

    def get_queryset(self):
        queryset = super().get_queryset()
        category_id = self.request.query_params.get('category')
        if category_id:
            queryset = queryset.filter(category_id=category_id)
        if self.action == 'list':
            queryset = apply_product_ordering(queryset, self.request.query_params.get('ordering'))
        return queryset

    @action(detail=True, methods=['get'])
    def similar(self, request, pk=None):
        product = self.get_object()
        try:
            limit = int(request.query_params.get('limit', SIMILAR_PRODUCTS_DEFAULT_LIMIT))
        except ValueError:
            limit = SIMILAR_PRODUCTS_DEFAULT_LIMIT
        limit = max(1, min(limit, SIMILAR_PRODUCTS_MAX_LIMIT))

        queryset = Product.objects.filter(category=product.category).exclude(pk=product.pk)

        if queryset.count() < limit and product.category.parent_id:
            sibling_categories = product.category.parent.get_descendants(include_self=True)
            queryset = Product.objects.filter(category__in=sibling_categories).exclude(pk=product.pk)

        queryset = queryset.select_related('category').annotate(
            price_diff=ExpressionWrapper(
                Abs(F('price') - product.price),
                output_field=DecimalField(max_digits=10, decimal_places=2),
            )
        ).order_by('price_diff')[:limit]

        serializer = ProductListSerializer(queryset, many=True, context={'request': request})
        return Response(serializer.data)
