import { AnimatePresence, motion } from "framer-motion";
import { Link } from "react-router-dom";
import { useCart } from "../store/CartContext";
import { money } from "../lib/money";

export default function CartDrawer() {
  const { items, open, setOpen, setQty, remove, subtotal } = useCart();

  return (
    <AnimatePresence>
      {open && (
        <>
          <motion.div
            className="fixed inset-0 z-50 bg-black/70 backdrop-blur-[2px]"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            onClick={() => setOpen(false)}
          />
          <motion.aside
            className="fixed right-0 top-0 bottom-0 z-50 w-full max-w-md bg-smoke border-l border-ash flex flex-col"
            initial={{ x: "100%" }}
            animate={{ x: 0 }}
            exit={{ x: "100%" }}
            transition={{ duration: 0.45, ease: [0.76, 0, 0.24, 1] }}
          >
            <div className="h-16 px-5 flex items-center justify-between border-b border-ash">
              <h2 className="font-display uppercase text-bone tracking-wide">
                Your Cart <span className="text-acid">({items.reduce((a, i) => a + i.qty, 0)})</span>
              </h2>
              <button aria-label="Close cart" onClick={() => setOpen(false)} className="text-bone text-3xl font-display leading-none px-1 hover:text-acid">
                ×
              </button>
            </div>

            <div className="flex-1 overflow-y-auto px-5 py-4 space-y-4">
              {items.length === 0 && (
                <div className="h-full flex flex-col items-center justify-center text-center gap-4 py-16">
                  <p className="display-xl text-4xl text-bone/20">Empty</p>
                  <p className="text-bone/50 text-sm uppercase tracking-widest">Nothing in the bag yet.</p>
                  <Link to="/shop" onClick={() => setOpen(false)} className="btn-og bg-acid text-ink px-6 py-3 text-xs mt-2 hover:bg-bone">
                    Shop the drop
                  </Link>
                </div>
              )}
              {items.map((i) => (
                <motion.div
                  layout
                  key={i.productId + (i.size || "")}
                  initial={{ opacity: 0, y: 10 }}
                  animate={{ opacity: 1, y: 0 }}
                  exit={{ opacity: 0 }}
                  className="flex gap-4 border border-ash bg-ink/60 p-3"
                >
                  <div className="w-20 h-24 bg-bone overflow-hidden shrink-0">
                    {i.image && <img src={i.image} alt={i.name} className="w-full h-full object-cover mix-blend-multiply" />}
                  </div>
                  <div className="flex-1 min-w-0">
                    <p className="font-display uppercase text-sm text-bone truncate">{i.name}</p>
                    {i.size && <p className="text-bone/50 text-xs mt-0.5 uppercase">Size {i.size}</p>}
                    <p className="text-acid text-sm mt-1">{money(i.price)}</p>
                    <div className="flex items-center gap-3 mt-2">
                      <div className="flex items-center border border-ash">
                        <button className="px-2.5 py-1 text-bone hover:text-acid" onClick={() => setQty(i.productId, i.size, i.qty - 1)}>−</button>
                        <span className="px-2 text-sm text-bone">{i.qty}</span>
                        <button className="px-2.5 py-1 text-bone hover:text-acid" onClick={() => setQty(i.productId, i.size, i.qty + 1)}>+</button>
                      </div>
                      <button className="text-bone/40 text-xs uppercase tracking-wider hover:text-blood" onClick={() => remove(i.productId, i.size)}>
                        Remove
                      </button>
                    </div>
                  </div>
                </motion.div>
              ))}
            </div>

            {items.length > 0 && (
              <div className="border-t border-ash p-5 space-y-4 bg-ink/40">
                <div className="flex justify-between font-display uppercase text-bone">
                  <span className="text-bone/60 text-sm tracking-widest">Subtotal</span>
                  <span className="text-acid">{money(subtotal)}</span>
                </div>
                <Link to="/checkout" onClick={() => setOpen(false)} className="btn-og w-full bg-acid text-ink py-4 text-sm hover:bg-bone">
                  Checkout →
                </Link>
                <p className="text-bone/30 text-[11px] text-center uppercase tracking-widest">Secure payment via Paystack</p>
              </div>
            )}
          </motion.aside>
        </>
      )}
    </AnimatePresence>
  );
}
