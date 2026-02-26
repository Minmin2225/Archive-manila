/* Archive Manila — Cart (localStorage) */

/** Return a usable <img src> from a DB image value (data-URI or filename). */
function imgSrc(value) {
  if (!value) return null;
  if (value.startsWith('data:')) return value;
  return '/static/uploads/' + value;
}

function getCart() {
  try { return JSON.parse(localStorage.getItem('am_cart') || '[]'); }
  catch { return []; }
}
function saveCart(cart) {
  localStorage.setItem('am_cart', JSON.stringify(cart));
  updateCartUI();
}
function addToCart(product) {
  const cart     = getCart();
  const existing = cart.find(i => i.id === product.id);
  if (existing) {
    existing.quantity = Math.min(existing.quantity + (product.quantity || 1), product.stock || 99);
  } else {
    cart.push({ ...product, quantity: product.quantity || 1 });
  }
  saveCart(cart);
}
function updateCartUI() {
  const cart  = getCart();
  const total = cart.reduce((s, i) => s + i.quantity, 0);
  const count = document.getElementById('cart-count');
  if (count) {
    count.textContent  = total;
    count.style.display = total > 0 ? 'flex' : 'none';
  }
  renderCartItems();
}
function renderCartItems() {
  const cart   = getCart();
  const list   = document.getElementById('cart-items-list');
  const footer = document.getElementById('cart-footer');
  if (!list) return;
  if (cart.length === 0) {
    list.innerHTML = '<div class="empty-cart">Your cart is empty</div>';
    if (footer) footer.style.display = 'none';
    return;
  }
  const subtotal = cart.reduce((s, i) => s + i.price * i.quantity, 0);
  list.innerHTML  = cart.map((item, idx) => {
    const src = imgSrc(item.image);
    return `
    <div class="cart-item">
      ${src
        ? `<img class="cart-item-img" src="${src}" alt="${item.name}">`
        : `<div class="cart-item-img" style="display:flex;align-items:center;justify-content:center;font-size:1.5rem;background:var(--light-gray)">👕</div>`}
      <div>
        <div class="cart-item-name">${item.name}</div>
        <div class="cart-item-price">₱${Math.round(item.price)}</div>
        <div class="qty-ctrl">
          <button class="qty-btn" onclick="changeQtyCart(${idx},-1)">−</button>
          <span class="qty-val">${item.quantity}</span>
          <button class="qty-btn" onclick="changeQtyCart(${idx},1)">+</button>
        </div>
      </div>
      <button class="remove-item" onclick="removeFromCart(${idx})">✕</button>
    </div>`;
  }).join('');
  if (footer) {
    footer.style.display = 'block';
    document.getElementById('cart-subtotal').textContent = `₱${Math.round(subtotal)}`;
  }
}
function changeQtyCart(idx, delta) {
  const cart = getCart();
  cart[idx].quantity = Math.max(1, cart[idx].quantity + delta);
  saveCart(cart);
}
function removeFromCart(idx) {
  const cart = getCart();
  cart.splice(idx, 1);
  saveCart(cart);
}
function clearCart() { saveCart([]); }
function toggleCart() {
  document.getElementById('cart-drawer').classList.toggle('open');
  document.getElementById('cart-overlay').classList.toggle('open');
}
function showToast(msg) {
  const tc = document.getElementById('toast-container');
  if (!tc) return;
  const t = document.createElement('div');
  t.className   = 'toast';
  t.textContent = msg;
  tc.appendChild(t);
  setTimeout(() => t.remove(), 3000);
}
document.addEventListener('DOMContentLoaded', updateCartUI);
