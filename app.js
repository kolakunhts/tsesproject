(function () {
      "use strict";

      var FREE_FROM = 200000;
      var KEYS = { cart: "kb_cart_v1", form: "kb_form_v1", theme: "kb_theme" };

      var cart = load(KEYS.cart, {});
      var form = load(KEYS.form, {});
      var toastTimer;

      function $(id) { return document.getElementById(id); }
      function load(key, fallback) {
            try { var value = localStorage.getItem(key); return value ? JSON.parse(value) : fallback; }
            catch (error) { return fallback; }
      }
      function save(key, value) {
            try { localStorage.setItem(key, JSON.stringify(value)); } catch (error) { }
      }
      function formatPrice(value) { return value.toLocaleString("en-US") + " ₭"; }
      function escapeHtml(value) {
            return String(value).replace(/[&<>"']/g, function (c) {
                  return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
            });
      }

      function showToast(message) {
            $("toast").textContent = message;
            $("toast").classList.add("show");
            clearTimeout(toastTimer);
            toastTimer = setTimeout(function () { $("toast").classList.remove("show"); }, 1800);
      }

      // 1. ຟັງຊັນເພີ່ມສິນຄ້າຈາກ HTML Card
      window.addToCart = function (id, name, price, image) {
            if (!cart[id]) {
                  cart[id] = { id: id, name: name, price: price, image: image, qty: 1 };
            } else {
                  cart[id].qty += 1;
            }
            save(KEYS.cart, cart);
            renderCart();
            showToast("ເພີ່ມ " + name + " ໃສ່ກະຕ່າແລ້ວ");
      };

      function changeQuantity(id, delta) {
            if (cart[id]) {
                  cart[id].qty += delta;
                  if (cart[id].qty <= 0) delete cart[id];
                  save(KEYS.cart, cart);
                  renderCart();
            }
      }

      function calculateCart() {
            var subtotal = 0;
            var count = 0;
            Object.keys(cart).forEach(function (id) {
                  subtotal += cart[id].price * cart[id].qty;
                  count += cart[id].qty;
            });
            var baseFee = Number($("zone").value);
            var fee = count ? (subtotal >= FREE_FROM ? 0 : baseFee) : 0;
            return { subtotal: subtotal, count: count, fee: fee, total: subtotal + fee };
      }

      function renderCart() {
            var ids = Object.keys(cart);
            $("cartList").innerHTML = ids.length ? ids.map(function (id) {
                  var item = cart[id];
                  return '<div class="line"><img class="cart-thumb" src="' + item.image + '" alt="' + escapeHtml(item.name) + '"><div><b>' + escapeHtml(item.name) +
                        '</b><span>' + formatPrice(item.price * item.qty) + '</span></div><div class="qty"><button data-dec="' +
                        id + '">−</button><output>' + item.qty + '</output><button data-inc="' + id + '">+</button></div></div>';
            }).join("") : '<div class="cart-empty">🛒<br>ກະຕ່າຍັງວ່າງ.</div>';

            var summary = calculateCart();
            $("sSub").textContent = formatPrice(summary.subtotal);
            $("sFee").textContent = summary.count ? (summary.fee ? formatPrice(summary.fee) : "ຟຣີ") : "0 ₭";
            $("sTotal").textContent = formatPrice(summary.total);
            $("cartBadge").textContent = summary.count;
            if ($("mobileCartBadge")) $("mobileCartBadge").textContent = summary.count;

            $("orderBtn").disabled = !summary.count;
            $("freeNote").innerHTML = summary.count ? '<div class="free-note">' +
                  (summary.fee ? "ສັ່ງເພີ່ມອີກ " + formatPrice(FREE_FROM - summary.subtotal) + " ເພື່ອສົ່ງຟຣີ" :
                        "🎉 ສັ່ງຄົບ " + formatPrice(FREE_FROM) + " — ສົ່ງຟຣີ") + "</div>" : "";
      }

      function toggleDrawer(open) {
            $("cart").classList.toggle("open", open);
            $("scrim").classList.toggle("open", open);
      }

      function validateForm() {
            var name = $("inName").value.trim();
            var phone = $("inPhone").value.replace(/\D/g, "");
            var address = $("inAddr").value.trim();
            $("fName").classList.toggle("err", !name);
            $("fPhone").classList.toggle("err", phone.length < 8);
            $("fAddr").classList.toggle("err", !address);
            return Boolean(name && phone.length >= 8 && address);
      }

      function showReceipt(order) {
            var rows = order.items.map(function (item) {
                  return '<div class="r-row"><span>' + escapeHtml(item.name) + ' × ' + item.qty +
                        '</span><span>' + formatPrice(item.price * item.qty) + '</span></div>';
            }).join("");
            $("receipt").innerHTML = '<h2>ໃບບິນ · 078food</h2><p class="sub">ເລກທີ່ ' + escapeHtml(order.no) +
                  '<br>' + escapeHtml(order.date) + '</p><hr><div><b>ຜູ້ຮັບ:</b> ' + escapeHtml(order.name) +
                  '<br><b>ໂທ:</b> ' + escapeHtml(order.phone) + '<br><b>ທີ່ຢູ່:</b> ' + escapeHtml(order.address) +
                  '<br><b>ເຂດ:</b> ' + escapeHtml(order.zone) + ' · ' + escapeHtml(order.payment) + '</div><hr>' + rows +
                  '<hr><div class="r-row"><span>ລາຄາອາຫານ</span><span>' + formatPrice(order.subtotal) +
                  '</span></div><div class="r-row"><span>ຄ່າຈັດສົ່ງ</span><span>' + (order.fee ? formatPrice(order.fee) : "ຟຣີ") +
                  '</span></div><div class="r-row b"><span>ລວມທັງໝົດ</span><span>' + formatPrice(order.total) +
                  '</span></div><hr><p style="text-align:center;">ຂອບໃຈທີ່ສັ່ງຊື້! 🍲</p><div class="r-actions">' +
                  '<button class="r-print" id="rPrint">ພິມໃບບິນ</button><button class="r-close" id="rClose">ປິດ</button></div>';
            $("modal").classList.add("open");
            $("rPrint").onclick = function () { window.print(); };
            $("rClose").onclick = function () { $("modal").classList.remove("open"); };
      }

      function placeOrder() {
            if (!validateForm()) { showToast("ກະລຸນາຕື່ມຂໍ້ມູນໃຫ້ຄົບ"); return; }
            var summary = calculateCart();
            var date = new Date();
            var order = {
                  no: "KB" + Date.now(), date: date.toLocaleString("lo-LA"),
                  items: Object.keys(cart).map(function (id) { return cart[id]; }),
                  subtotal: summary.subtotal, fee: summary.fee, total: summary.total,
                  name: $("inName").value, phone: $("inPhone").value, address: $("inAddr").value,
                  zone: $("zone").selectedOptions[0].text, payment: $("pay").value
            };
            showReceipt(order);
            cart = {};
            save(KEYS.cart, cart);
            renderCart();
            toggleDrawer(false);
      }

      function filterMenu() {
            var activeCat = document.querySelector(".cat.active")?.dataset.cat || "ທັງໝົດ";
            var searchText = $("search").value.trim().toLowerCase();
            var cards = document.querySelectorAll(".grid .food");

            cards.forEach(function (card) {
                  var cat = card.dataset.cat;
                  var title = card.querySelector("h3").textContent.toLowerCase();
                  var matchCat = (activeCat === "ທັງໝົດ" || cat === activeCat);
                  var matchSearch = (!searchText || title.includes(searchText));

                  if (matchCat && matchSearch) {
                        card.style.display = "";
                  } else {
                        card.style.display = "none";
                  }
            });
      }

      function bindEvents() {
            $("themeBtn").onclick = function () {
                  var dark = document.documentElement.getAttribute("data-theme") === "dark";
                  var theme = dark ? "light" : "dark";
                  document.documentElement.setAttribute("data-theme", theme);
                  save(KEYS.theme, theme);
            };

            $("cats").onclick = function (e) {
                  var btn = e.target.closest(".cat");
                  if (btn) {
                        document.querySelectorAll(".cat").forEach(function (b) { b.classList.remove("active"); });
                        btn.classList.add("active");
                        filterMenu();
                  }
            };

            $("search").oninput = filterMenu;

            $("cartList").onclick = function (e) {
                  var inc = e.target.closest("[data-inc]");
                  var dec = e.target.closest("[data-dec]");
                  if (inc) changeQuantity(inc.dataset.inc, 1);
                  if (dec) changeQuantity(dec.dataset.dec, -1);
            };

            $("clearBtn").onclick = function () { cart = {}; save(KEYS.cart, cart); renderCart(); };
            $("zone").onchange = renderCart;
            $("cartToggle").onclick = function () { toggleDrawer(true); };
            if ($("mobileCartBtn")) {
                  $("mobileCartBtn").onclick = function () { toggleDrawer(true); };
            }
            $("cartClose").onclick = $("scrim").onclick = function () { toggleDrawer(false); };
            $("orderBtn").onclick = placeOrder;
            $("modal").onclick = function (e) { if (e.target === $("modal")) $("modal").classList.remove("open"); };

            ["inName", "inPhone", "inAddr"].forEach(function (id) {
                  if ($(id)) {
                        $(id).oninput = function () { form[id] = $(id).value; save(KEYS.form, form); };
                  }
            });
      }

      function initialize() {
            var theme = load(KEYS.theme, null);
            if (theme) document.documentElement.setAttribute("data-theme", theme);
            ["inName", "inPhone", "inAddr"].forEach(function (id) { if (form[id] && $(id)) $(id).value = form[id]; });
            bindEvents();
            renderCart();
      }

      initialize();
})();