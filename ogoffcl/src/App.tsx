import { Routes, Route, useLocation } from "react-router-dom";
import { useEffect } from "react";
import LockGate from "./components/LockGate";
import Navbar from "./components/Navbar";
import CartDrawer from "./components/CartDrawer";
import Footer from "./components/Footer";
import Home from "./pages/Home";
import Shop from "./pages/Shop";
import ProductPage from "./pages/ProductPage";
import Gallery from "./pages/Gallery";
import Tournament from "./pages/Tournament";
import Fixtures from "./pages/Fixtures";
import Checkout from "./pages/Checkout";
import Confirmation from "./pages/Confirmation";
import RefundPolicy from "./pages/RefundPolicy";
import NotFound from "./pages/NotFound";
import AdminLayout from "./admin/AdminLayout";
import Dashboard from "./admin/Dashboard";
import AdminProducts from "./admin/Products";
import AdminOrders from "./admin/Orders";
import AdminCategories from "./admin/Categories";
import AdminDiscounts from "./admin/Discounts";
import AdminGallery from "./admin/Gallery";
import AdminSiteMail from "./admin/SiteMail";
import AdminEmail from "./admin/Email";
import AdminAnalytics from "./admin/Analytics";
import AdminTournament from "./admin/Tournament";
import { usePageTracking } from "./lib/track";

function ScrollTop() {
  const { pathname } = useLocation();
  useEffect(() => { window.scrollTo({ top: 0 }); }, [pathname]);
  return null;
}

function Tracker() {
  usePageTracking();
  return null;
}

export default function App() {
  return (
    <LockGate>
      <div className="noise min-h-screen flex flex-col">
        <ScrollTop />
        <Tracker />
        <Navbar />
        <CartDrawer />
        <main className="flex-1">
          <Routes>
            <Route path="/" element={<Home />} />
            <Route path="/shop" element={<Shop />} />
            <Route path="/product/:id" element={<ProductPage />} />
            <Route path="/gallery" element={<Gallery />} />
            <Route path="/tournament" element={<Tournament />} />
            <Route path="/tournament/fixtures" element={<Fixtures />} />
            <Route path="/checkout" element={<Checkout />} />
            <Route path="/order-confirmation" element={<Confirmation />} />
            <Route path="/refund-policy" element={<RefundPolicy />} />
            <Route path="/admin" element={<AdminLayout />}>
              <Route index element={<Dashboard />} />
              <Route path="products" element={<AdminProducts />} />
              <Route path="orders" element={<AdminOrders />} />
              <Route path="categories" element={<AdminCategories />} />
              <Route path="discounts" element={<AdminDiscounts />} />
              <Route path="gallery" element={<AdminGallery />} />
              <Route path="site" element={<AdminSiteMail />} />
              <Route path="email" element={<AdminEmail />} />
              <Route path="analytics" element={<AdminAnalytics />} />
              <Route path="tournament" element={<AdminTournament />} />
            </Route>
            <Route path="*" element={<NotFound />} />
          </Routes>
        </main>
        <Footer />
      </div>
    </LockGate>
  );
}
