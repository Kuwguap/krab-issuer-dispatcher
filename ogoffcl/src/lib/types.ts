export interface Product {
  id: string;
  name: string;
  description: string | null;
  price: number;
  image: string | null;
  images: string[] | null;
  category_id: string | null;
  subcategory_id: string | null;
  product_category: string | null;
  sizes: string[] | null;
  stock: number | null;
  sku: string | null;
  is_active: boolean;
  is_reserved: boolean | null;
  created_at?: string;
}

export interface Category {
  id: string;
  name: string;
  slug: string;
  description?: string | null;
}

export interface CartItem {
  productId: string;
  name: string;
  price: number;
  image: string;
  size: string | null;
  qty: number;
}

export interface OrderRow {
  id: string;
  order_number: string;
  status: string;
  payment_status: string;
  payment_method?: string | null;
  total_amount: number;
  currency?: string | null;
  customer_name: string | null;
  customer_email: string | null;
  customer_phone: string | null;
  shipping_address: string | null;
  discount_code?: string | null;
  discount_amount?: number | null;
  created_at: string;
}

export interface DiscountCode {
  id: string;
  code: string;
  percentage: number | null;
  is_active: boolean;
  expires_at: string | null;
  [key: string]: unknown;
}
