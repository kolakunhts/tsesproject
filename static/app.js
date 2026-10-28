
(function () {
      "use strict";

      // ---------- Configuration ----------
      var FREE_FROM = 200000;

      var KEYS = {
            cart: "kb_cart_v1",
            form: "kb_form_v1",
            theme: "kb_theme"
      };

      // ---------- State ----------
      var cart = load(KEYS.cart, {});
      var form = load(KEYS.form, {});
      var foodItems = [];
      var toastTimer = null;
      var transferConfirmed = false;
      var isSubmittingOrder = false;
      var isAuthenticated = document.body.dataset.authenticated === "true";

      // ---------- Helpers ----------
      function $(id) {
            return document.getElementById(id);
      }

      function csrfToken() {
            var meta = document.querySelector('meta[name="csrf-token"]');
            return meta ? meta.getAttribute("content") : "";
      }

      function load(key, fallback) {
            try {
                  var value = localStorage.getItem(key);
                  return value ? JSON.parse(value) : fallback;
            } catch (error) {
                  return fallback;
            }
      }

      function save(key, value) {
            try {
                  localStorage.setItem(key, JSON.stringify(value));
            } catch (error) {
                  console.error("Cannot save to localStorage:", error);
            }
      }

      function formatPrice(value) {
            return Number(value || 0).toLocaleString("en-US") + " ₭";
      }

      function escapeHtml(value) {
            return String(value == null ? "" : value).replace(
                  /[&<>"']/g,
                  function (character) {
                        return {
                              "&": "&amp;",
                              "<": "&lt;",
                              ">": "&gt;",
                              '"': "&quot;",
                              "'": "&#39;"
                        }[character];
                  }
            );
      }

      function showToast(message) {
            var toast = $("toast");
            if (!toast) return;

            toast.textContent = message;
            toast.classList.add("show");

            clearTimeout(toastTimer);

            toastTimer = setTimeout(function () {
                  toast.classList.remove("show");
            }, 1800);
      }

      function setOrderButtonLabel(label, loading) {
            var button = $("orderBtn");
            if (!button) return;
            button.innerHTML = loading
                  ? '<span class="button-spinner" aria-hidden="true"></span><span>' + escapeHtml(label) + '</span>'
                  : '<i class="fa-solid fa-arrow-right" aria-hidden="true"></i><span>' + escapeHtml(label) + '</span>';
      }

      function syncCartWithMenu() {
            var changed = false;
            Object.keys(cart).forEach(function (id) {
                  var current = foodItems.find(function (item) {
                        return String(item.id) === String(id);
                  });
                  if (!current || current.stock <= 0) {
                        delete cart[id];
                        changed = true;
                        return;
                  }
                  var safeQty = Math.min(Math.max(1, Number(cart[id].qty) || 1), current.stock);
                  if (cart[id].name !== current.name || cart[id].price !== current.price || cart[id].image !== current.image || cart[id].qty !== safeQty) {
                        cart[id].name = current.name;
                        cart[id].price = current.price;
                        cart[id].image = current.image;
                        cart[id].qty = safeQty;
                        changed = true;
                  }
            });
            if (changed) save(KEYS.cart, cart);
      }

      function bindImageFallbacks(root) {
            if (!root) return;
            root.querySelectorAll("img[data-fallback]").forEach(function (image) {
                  image.addEventListener("error", function () {
                        var fallback = document.createElement("div");
                        fallback.className = image.dataset.fallback;
                        fallback.textContent = "🍲";
                        image.replaceWith(fallback);
                  }, { once: true });
            });
      }

      // ---------- Load products from Flask API ----------
      async function loadMenu() {
            var grid = $("grid");
            if (!grid) return;

            grid.innerHTML = '<div class="menu-loading" role="status"><span class="loading-dots"></span><strong>ກຳລັງຈັດເມນູໃຫ້ທ່ານ...</strong><small>ກະລຸນາລໍຖ້າໜ້ອຍໜຶ່ງ</small></div>';

            try {
                  var response = await fetch("/api/products", {
                        method: "GET",
                        headers: {
                              "Accept": "application/json"
                        }
                  });

                  if (!response.ok) {
                        throw new Error("HTTP " + response.status);
                  }

                  var products = await response.json();

                  if (!Array.isArray(products)) {
                        throw new Error("Invalid products response");
                  }

                  foodItems = products
                        .filter(function (product) {
                              return (
                                    product.id != null &&
                                    product.name &&
                                    Number.isFinite(Number(product.price)) &&
                                    Number(product.price) >= 0
                              );
                        })
                        .map(function (product) {
                              return {
                                    id: Number(product.id),
                                    name: String(product.name),
                                    category: String(product.category || "ອື່ນໆ"),
                                    description: String(product.description || ""),
                                    price: Number(product.price),
                                    stock: Math.max(0, Number(product.stock) || 0),
                                    image: String(product.image || "")
                              };
                        });

                  // Keep compatibility with other existing scripts if needed.
                  window.FOOD_ITEMS = foodItems;

                  renderMenu();
                  renderCategories();
                  syncCartWithMenu();
                  renderCart();
                  filterMenu();
                  if ($("menuCount")) $("menuCount").textContent = foodItems.length + " ລາຍການຈາກເມນູຈິງ";

            } catch (error) {
                  console.error("Load menu error:", error);

                  grid.innerHTML =
                        '<div class="menu-error">' +
                        "<p>ໂຫຼດເມນູບໍ່ສຳເລັດ.</p>" +
                        '<button type="button" id="retryMenu">ລອງໃໝ່</button>' +
                        "</div>";
                  if ($("cats")) $("cats").innerHTML = '<span class="categories-loading">ໂຫຼດໝວດອາຫານບໍ່ສຳເລັດ</span>';
                  if ($("menuCount")) $("menuCount").textContent = "ໂຫຼດເມນູບໍ່ສຳເລັດ";

                  var retryButton = $("retryMenu");

                  if (retryButton) {
                        retryButton.onclick = loadMenu;
                  }

                  showToast("ບໍ່ສາມາດໂຫຼດເມນູໄດ້");
            }
      }

      // ---------- Render menu ----------
      function renderMenu() {
            var grid = $("grid");
            if (!grid) return;

            if (!foodItems.length) {
                  grid.innerHTML =
                        '<div class="menu-empty">' +
                        '<div class="empty-menu-icon">🍽️</div>' +
                        "<strong>ຍັງບໍ່ມີລາຍການອາຫານ</strong>" +
                        "<p>ກະລຸນາກັບມາກວດເບິ່ງອີກຄັ້ງ.</p>" +
                        "</div>";
                  return;
            }

            grid.innerHTML = foodItems.map(function (item) {
                  var imageMarkup = item.image
                        ? '<img class="food-image" data-fallback="food-no-image" src="' + escapeHtml(item.image) +
                        '" alt="' + escapeHtml(item.name) +
                        '" loading="lazy">'
                        : '<div class="food-no-image">🍲</div>';

                  var outOfStock = item.stock <= 0;

                  return (
                        '<article class="food" data-id="' + item.id +
                        '" data-cat="' + escapeHtml(item.category) + '">' +

                        '<div class="food-pic">' +
                        '<small>' + escapeHtml(item.category) + '</small>' +
                        imageMarkup +
                        '<span class="food-stock-pill ' + (outOfStock ? 'is-out' : '') + '">' +
                        '<i class="fa-solid ' + (outOfStock ? 'fa-box-open' : 'fa-circle-check') + '" aria-hidden="true"></i> ' +
                        (outOfStock ? 'ໝົດສະຕັອກ' : 'ມີສິນຄ້າ') + '</span>' +
                        '</div>' +

                        '<div class="food-body">' +
                        '<h3>' + escapeHtml(item.name) + '</h3>' +
                        '<p class="desc">' +
                        escapeHtml(item.description || "ປຸງສົດຕາມອໍເດີ ແລະຄັດສັນວັດຖຸດິບຄຸນນະພາບ") +
                        '</p>' +

                        '<div class="food-foot">' +
                        '<span class="price">' +
                        formatPrice(item.price) +
                        '</span>' +

                        '<button class="add" type="button" aria-label="ເພີ່ມ ' + escapeHtml(item.name) + ' ໃສ່ກະຕ່າ"' +
                        ' data-add="' + item.id + '"' +
                        (outOfStock ? " disabled" : "") +
                        '>' +
                        (outOfStock
                              ? '<i class="fa-solid fa-box-open" aria-hidden="true"></i><span>ໝົດສະຕັອກ</span>'
                              : '<i class="fa-solid fa-cart-plus" aria-hidden="true"></i><span>ເພີ່ມໃສ່ກະຕ່າ</span>') +
                        '</button>' +
                        '</div>' +
                        '</div>' +

                        '</article>'
                  );
            }).join("");
            bindImageFallbacks(grid);
      }

      // ---------- Render category filters ----------
      function renderCategories() {
            var cats = $("cats");
            if (!cats) return;

            var currentCategory =
                  cats.querySelector(".cat.active")?.dataset.cat || "ທັງໝົດ";

            var categories = Array.from(
                  new Set(
                        foodItems.map(function (item) {
                              return item.category;
                        })
                  )
            ).filter(function (cat) {
                  return cat && cat !== "ທັງໝົດ";
            });

            var allCategories = ["ທັງໝົດ"].concat(categories);

            cats.innerHTML = allCategories.map(function (category) {
                  var categoryItem = category === "ທັງໝົດ"
                        ? foodItems[0]
                        : foodItems.find(function (item) { return item.category === category; });
                  var categoryImage = categoryItem && categoryItem.image
                        ? '<img data-fallback="category-placeholder" src="' + escapeHtml(categoryItem.image) + '" alt="" loading="lazy">'
                        : '<span class="category-placeholder">🍲</span>';
                  var active = category === currentCategory ||
                        (currentCategory === "ທັງໝົດ" &&
                              category === "ທັງໝົດ");

                  return (
                        '<button type="button" class="cat' +
                        (active ? " active" : "") +
                        '" data-cat="' + escapeHtml(category) + '">' +
                        '<span class="category-image">' + categoryImage + '</span>' +
                        '<span class="category-name">' + escapeHtml(category) + '</span>' +
                        "</button>"
                  );
            }).join("");
            bindImageFallbacks(cats);
      }

      // ---------- Cart ----------
      window.addToCart = function (id, name, price, image) {
            var item = foodItems.find(function (food) {
                  return String(food.id) === String(id);
            });

            if (!item) {
                  showToast("ບໍ່ພົບລາຍການອາຫານ");
                  return;
            }

            if (item.stock <= 0) {
                  showToast("ອາຫານນີ້ໝົດສະຕັອກແລ້ວ");
                  return;
            }

            var key = String(item.id);

            if (!cart[key]) {
                  cart[key] = {
                        id: item.id,
                        name: item.name,
                        price: item.price,
                        image: item.image,
                        qty: 1
                  };
            } else {
                  if (cart[key].qty >= item.stock) {
                        showToast("ຈຳນວນສິນຄ້າບໍ່ພຽງພໍ");
                        return;
                  }

                  cart[key].qty += 1;
            }

            save(KEYS.cart, cart);
            renderCart();

            showToast("ເພີ່ມ " + item.name + " ໃສ່ກະຕ່າແລ້ວ");
      };

      function changeQuantity(id, delta) {
            var key = String(id);
            if (!cart[key]) return;

            var item = foodItems.find(function (food) {
                  return String(food.id) === key;
            });

            var newQuantity = cart[key].qty + delta;

            if (newQuantity <= 0) {
                  delete cart[key];
            } else if (item && newQuantity > item.stock) {
                  showToast("ຈຳນວນສິນຄ້າບໍ່ພຽງພໍ");
                  return;
            } else {
                  cart[key].qty = newQuantity;
            }

            save(KEYS.cart, cart);
            renderCart();
      }

      function calculateCart() {
            var subtotal = 0;
            var count = 0;

            Object.keys(cart).forEach(function (id) {
                  var item = cart[id];

                  subtotal += Number(item.price) * Number(item.qty);
                  count += Number(item.qty);
            });

            var zone = $("zone");
            var baseFee = zone ? Number(zone.value) || 0 : 0;

            var fee = count
                  ? (subtotal >= FREE_FROM ? 0 : baseFee)
                  : 0;

            return {
                  subtotal: subtotal,
                  count: count,
                  fee: fee,
                  total: subtotal + fee
            };
      }

      function updateTransferInfo() {
            var transferInfo = $("transferInfo");
            var payment = $("pay");
            if (!payment) return;

            if (transferInfo) {
                  transferInfo.hidden = payment.value !== "ໂອນ / QR";
            }

            if ($("orderBtn")) {
                  setOrderButtonLabel(
                        payment.value === "ໂອນ / QR"
                              ? "ຂ້ອຍໂອນແລ້ວ · ສ້າງໃບບິນ"
                              : "ສັ່ງຊື້ເລີຍ",
                        false
                  );
            }
      }

      function renderCart() {
            var cartList = $("cartList");
            if (!cartList) return;

            var ids = Object.keys(cart);

            cartList.innerHTML = ids.length
                  ? ids.map(function (id) {
                        var item = cart[id];

                        var imageMarkup = item.image
                              ? '<img class="cart-thumb" data-fallback="cart-thumb-placeholder" src="' +
                              escapeHtml(item.image) +
                              '" alt="' + escapeHtml(item.name) + '">'
                              : '<div class="cart-thumb-placeholder">🍲</div>';

                        return (
                              '<div class="line">' +
                              imageMarkup +

                              '<div>' +
                              '<b>' + escapeHtml(item.name) + '</b>' +
                              '<span class="line-unit">' + formatPrice(item.price) + ' / ຈານ</span>' +
                              '<strong class="line-total">' + formatPrice(item.price * item.qty) + '</strong>' +
                              '<button type="button" class="line-remove" data-remove="' + escapeHtml(id) + '" aria-label="ລຶບ ' + escapeHtml(item.name) + '"><i class="fa-regular fa-trash-can" aria-hidden="true"></i><span>ລຶບ</span></button>' +
                              '</div>' +

                              '<div class="qty">' +
                              '<button type="button" data-dec="' +
                              escapeHtml(id) + '" aria-label="ຫຼຸດຈຳນວນ"><i class="fa-solid fa-minus" aria-hidden="true"></i></button>' +
                              '<output>' + item.qty + '</output>' +
                              '<button type="button" data-inc="' +
                              escapeHtml(id) + '" aria-label="ເພີ່ມຈຳນວນ"><i class="fa-solid fa-plus" aria-hidden="true"></i></button>' +
                              '</div>' +
                              '</div>'
                        );
                  }).join("")
                  : '<div class="cart-empty"><div class="empty-cart-icon">🛒</div>' +
                  '<strong>ກະຕ່າຍັງວ່າງ</strong>' +
                  '<span>ເລືອກເມນູແຊບໆແລ້ວກັບມາທີ່ນີ້</span>' +
                  '<button type="button" class="empty-cart-action" data-browse-menu>ໄປເລືອກອາຫານ</button></div>';

            bindImageFallbacks(cartList);
            var summary = calculateCart();

            if ($("sSub")) {
                  $("sSub").textContent = formatPrice(summary.subtotal);
            }

            if ($("sFee")) {
                  $("sFee").textContent = summary.count
                        ? (summary.fee ? formatPrice(summary.fee) : "ຟຣີ")
                        : "0 ₭";
            }

            if ($("sTotal")) {
                  $("sTotal").textContent = formatPrice(summary.total);
            }

            if ($("headerCartTotal")) {
                  $("headerCartTotal").textContent = formatPrice(summary.total);
            }

            if ($("cartBadge")) {
                  $("cartBadge").textContent = summary.count;
            }

            if ($("cartItemCount")) {
                  $("cartItemCount").textContent = summary.count;
            }

            if ($("cartLineCount")) {
                  $("cartLineCount").textContent = ids.length;
            }

            if ($("mobileCartBadge")) {
                  $("mobileCartBadge").textContent = summary.count;
            }

            if ($("orderBtn")) {
                  $("orderBtn").disabled = !summary.count;
            }

            if ($("freeNote")) {
                  $("freeNote").innerHTML = summary.count
                        ? '<div class="free-note">' +
                        '<div class="free-note-head"><span><i class="fa-solid fa-gift" aria-hidden="true"></i> ສົ່ງຟຣີເມື່ອສັ່ງຄົບ ' + formatPrice(FREE_FROM) + '</span>' +
                        '<strong>' + (summary.fee ? formatPrice(FREE_FROM - summary.subtotal) + ' ອີກ' : 'ສຳເລັດ') + '</strong></div>' +
                        '<div class="free-track"><i style="width:' + Math.min(100, Math.round((summary.subtotal / FREE_FROM) * 100)) + '%"></i></div>' +
                        '<small>' + (summary.fee ? 'ສັ່ງເພີ່ມອີກເພື່ອຮັບສິດສົ່ງຟຣີ' : 'ທ່ານໄດ້ຮັບສິດສົ່ງຟຣີແລ້ວ') + '</small></div>'
                        : "";
            }
      }

      // ---------- Cart drawer ----------
      function toggleDrawer(open) {
            if ($("cart")) {
                  $("cart").classList.toggle("open", open);
                  $("cart").setAttribute("aria-hidden", open ? "false" : "true");
            }

            if ($("scrim")) {
                  $("scrim").classList.toggle("open", open);
            }

            document.body.classList.toggle("drawer-open", open);
            if ($("cartToggle")) {
                  $("cartToggle").setAttribute("aria-expanded", open ? "true" : "false");
            }
            if ($("mobileCartBtn")) {
                  $("mobileCartBtn").setAttribute("aria-expanded", open ? "true" : "false");
            }
      }

      // ---------- Checkout form ----------
      function validateForm() {
            var name = $("inName") ? $("inName").value.trim() : "";
            var phone = $("inPhone")
                  ? $("inPhone").value.replace(/\D/g, "")
                  : "";
            var address = $("inAddr") ? $("inAddr").value.trim() : "";

            if ($("fName")) {
                  $("fName").classList.toggle("err", !name);
            }

            if ($("fPhone")) {
                  $("fPhone").classList.toggle("err", phone.length < 8);
            }

            if ($("fAddr")) {
                  $("fAddr").classList.toggle("err", !address);
            }

            return Boolean(name && phone.length >= 8 && address);
      }

      // ---------- Receipt ----------
      function showReceipt(order) {
            var receipt = $("receipt");
            if (!receipt) return;

            var rows = order.items.map(function (item) {
                  return (
                        '<div class="r-row">' +
                        '<span>' + escapeHtml(item.name) +
                        " × " + item.qty + '</span>' +
                        '<span>' +
                        formatPrice(item.price * item.qty) +
                        '</span>' +
                        '</div>'
                  );
            }).join("");

            receipt.innerHTML =
                  '<h2>ໃບບິນ · 078food</h2>' +
                  '<p class="sub">ເລກທີ່ ' + escapeHtml(order.no) +
                  '<br>' + escapeHtml(order.date) + '</p>' +
                  '<hr>' +

                  '<div><b>ຜູ້ຮັບ:</b> ' + escapeHtml(order.name) +
                  '<br><b>ໂທ:</b> ' + escapeHtml(order.phone) +
                  '<br><b>ທີ່ຢູ່:</b> ' + escapeHtml(order.address) +
                  '<br><b>ເຂດ:</b> ' + escapeHtml(order.zone) +
                  ' · ' + escapeHtml(order.payment) + '</div>' +

                  '<hr>' + rows + '<hr>' +

                  '<div class="r-row"><span>ລາຄາອາຫານ</span>' +
                  '<span>' + formatPrice(order.subtotal) + '</span>' +
                  '</div>' +

                  '<div class="r-row"><span>ຄ່າຈັດສົ່ງ</span>' +
                  '<span>' +
                  (order.fee ? formatPrice(order.fee) : "ຟຣີ") +
                  '</span>' +
                  '</div>' +

                  '<div class="r-row b"><span>ລວມທັງໝົດ</span>' +
                  '<span>' + formatPrice(order.total) + '</span>' +
                  '</div>' +

                  (order.paymentStatus === "pending"
                        ? ((order.paymentMethod === "cash" || String(order.payment || "").indexOf("ສົດ") !== -1)
                              ? '<div class="payment-pending">' +
                              '<p><i class="fa-solid fa-money-bill-wave"></i> ສະຖານະ: ຊຳລະເງິນສົດປາຍທາງເມື່ອຮັບອາຫານ</p>' +
                              '</div>'
                              : '<div class="payment-pending">' +
                              '<p><i class="fa-solid fa-clock"></i> ສະຖານະ: ລໍຖ້າກວດສອບການຊຳລະ</p>' +
                              '<label class="slip-input-label"><span>ອັບໂຫຼດສະລິບ:</span> <input type="file" id="slipFile" accept=".jpg,.jpeg,.png,.pdf"></label>' +
                              '<button type="button" id="uploadSlip" class="btn-upload-slip"><i class="fa-solid fa-arrow-up-from-bracket"></i> ສົ່ງສະລິບ</button>' +
                              '<span id="slipMessage"></span>' +
                              '</div>')
                        : '') +

                  '<hr><p style="text-align:center; font-weight:600; color:var(--green);">' +
                  'ຂອບໃຈທີ່ສັ່ງຊື້ກັບ 078food! 🍲' +
                  '</p>' +

                  '<div class="r-actions">' +
                  '<button class="r-print" id="rPrint"><i class="fa-solid fa-print"></i> ພິມໃບບິນ</button>' +
                  (isAuthenticated
                        ? '<a class="r-btn r-orders-link" href="/orders"><i class="fa-solid fa-receipt"></i> ເບິ່ງປະຫວັດອໍເດີ</a>'
                        : '') +
                  '<button class="r-close" id="rClose">ປິດ</button>' +
                  '</div>';

            if ($("modal")) {
                  $("modal").classList.add("open");
            }

            if ($("rPrint")) {
                  $("rPrint").onclick = function () {
                        window.print();
                  };
            }

            if ($("rClose")) {
                  $("rClose").onclick = function () {
                        $("modal").classList.remove("open");
                  };
            }

            if ($("uploadSlip")) {
                  $("uploadSlip").onclick = function () {
                        uploadPaymentSlip(order.id);
                  };
            }
      }

      function openTransferModal() {
            var summary = calculateCart();
            if ($("transferTotal")) {
                  $("transferTotal").textContent = formatPrice(summary.total);
            }
            if ($("transferModal")) {
                  $("transferModal").classList.add("open");
            }
      }

      function closeTransferModal() {
            if ($("transferModal")) {
                  $("transferModal").classList.remove("open");
            }
      }

      async function uploadPaymentSlip(orderId) {
            var input = $("slipFile");
            var message = $("slipMessage");
            if (!input || !input.files.length) {
                  if (message) message.textContent = " ກະລຸນາເລືອກໄຟລ໌";
                  return;
            }

            var formData = new FormData();
            formData.append("slip", input.files[0]);

            try {
                  var response = await fetch("/api/orders/" + orderId + "/slip", {
                        method: "POST",
                        headers: { "X-CSRFToken": csrfToken() },
                        body: formData
                  });
                  var result = await response.json();
                  if (!response.ok) throw new Error(result.error || "ອັບໂຫຼດບໍ່ສຳເລັດ");
                  if (message) message.textContent = " " + result.message;
            } catch (error) {
                  if (message) message.textContent = " " + error.message;
            }
      }

      // ---------- Create order in Flask/SQLite ----------
      async function placeOrder() {
            if (isSubmittingOrder) return;
            if (!validateForm()) {
                  showToast("ກະລຸນາຕື່ມຂໍ້ມູນໃຫ້ຄົບ");
                  return;
            }

            if (!Object.keys(cart).length) {
                  showToast("ກະລຸນາເລືອກອາຫານກ່ອນ");
                  return;
            }

            if ($("pay") && $("pay").value === "ໂອນ / QR" && !transferConfirmed) {
                  openTransferModal();
                  return;
            }
            transferConfirmed = false;

            var summary = calculateCart();
            var orderButton = $("orderBtn");
            isSubmittingOrder = true;
            if (orderButton) {
                  orderButton.disabled = true;
                  orderButton.setAttribute("aria-busy", "true");
                  setOrderButtonLabel("ກຳລັງຢືນຢັນອໍເດີ...", true);
            }

            try {
                  var response = await fetch("/api/orders", {
                        method: "POST",
                        headers: {
                              "Content-Type": "application/json",
                              "Accept": "application/json",
                              "X-CSRFToken": csrfToken()
                        },
                        body: JSON.stringify({
                              name: $("inName").value.trim(),
                              phone: $("inPhone").value.trim(),
                              address: $("inAddr").value.trim(),
                              shipping_fee: Number($("zone").value) || 0,
                              payment: $("pay").value,
                              items: Object.keys(cart).map(function (id) {
                                    return { id: cart[id].id, qty: cart[id].qty };
                              })
                        })
                  });

                  var result = await response.json();
                  if (!response.ok) {
                        throw new Error(result.error || "ບັນທຶກອໍເດີບໍ່ສຳເລັດ");
                  }

                  if (result.checkout_url) {
                        window.location.assign(result.checkout_url);
                        return;
                  }

                  showReceipt(result);
                  cart = {};
                  save(KEYS.cart, cart);
                  renderCart();
                  toggleDrawer(false);
                  loadMenu();
            } catch (error) {
                  console.error("Create order error:", error);
                  showToast(error.message || "ບັນທຶກອໍເດີບໍ່ສຳເລັດ");
            } finally {
                  isSubmittingOrder = false;
                  if (orderButton) {
                        orderButton.disabled = !Object.keys(cart).length;
                        orderButton.removeAttribute("aria-busy");
                        setOrderButtonLabel(
                              $("pay") && $("pay").value === "ໂອນ / QR"
                                    ? "ຂ້ອຍໂອນແລ້ວ · ສ້າງໃບບິນ"
                                    : "ສັ່ງຊື້ເລີຍ",
                              false
                        );
                  }
            }
      }

      // ---------- Search and category filtering ----------
      function filterMenu() {
            var activeButton = document.querySelector(".cat.active");

            var activeCat = activeButton
                  ? activeButton.dataset.cat
                  : "ທັງໝົດ";

            var searchInput = $("search");
            var searchText = searchInput
                  ? searchInput.value.trim().toLowerCase()
                  : "";

            var cards = document.querySelectorAll(".grid .food");
            var visibleCount = 0;

            cards.forEach(function (card) {
                  var cat = card.dataset.cat;
                  var cardText = card.textContent.toLowerCase();

                  var matchCat =
                        activeCat === "ທັງໝົດ" || cat === activeCat;

                  var matchSearch =
                        !searchText || cardText.includes(searchText);

                  card.style.display =
                        matchCat && matchSearch ? "" : "none";
                  if (matchCat && matchSearch) visibleCount += 1;
            });

            var empty = $("filterEmpty");
            if (!visibleCount && cards.length) {
                  if (!empty) {
                        empty = document.createElement("div");
                        empty.id = "filterEmpty";
                        empty.className = "menu-filter-empty";
                        empty.innerHTML = '<div class="empty-menu-icon">🔎</div><strong>ບໍ່ພົບເມນູທີ່ຄົ້ນຫາ</strong><p>ລອງປ່ຽນຄຳຄົ້ນຫາ ຫຼືເລືອກໝວດອື່ນ.</p>';
                        $("grid").appendChild(empty);
                  }
            } else if (empty) {
                  empty.remove();
            }
      }

      // ---------- Events ----------
      function bindEvents() {
            if ($("themeBtn")) {
                  $("themeBtn").onclick = function () {
                        var dark =
                              document.documentElement.getAttribute("data-theme") ===
                              "dark";

                        var theme = dark ? "light" : "dark";

                        document.documentElement.setAttribute("data-theme", theme);
                        save(KEYS.theme, theme);
                  };
            }

            if ($("cats")) {
                  $("cats").onclick = function (event) {
                        var button = event.target.closest(".cat");
                        if (!button) return;

                        document.querySelectorAll(".cat").forEach(function (item) {
                              item.classList.remove("active");
                        });

                        button.classList.add("active");
                        filterMenu();
                  };
            }

            if ($("search")) {
                  $("search").oninput = filterMenu;
            }

            if ($("grid")) {
                  $("grid").onclick = function (event) {
                        var button = event.target.closest("[data-add]");
                        if (!button || button.disabled) return;

                        var item = foodItems.find(function (food) {
                              return String(food.id) === button.dataset.add;
                        });

                        if (item) {
                              window.addToCart(
                                    item.id,
                                    item.name,
                                    item.price,
                                    item.image
                              );
                        }
                  };
            }

            if ($("cartList")) {
                  $("cartList").onclick = function (event) {
                        var inc = event.target.closest("[data-inc]");
                        var dec = event.target.closest("[data-dec]");

                        if (inc) {
                              changeQuantity(inc.dataset.inc, 1);
                        }

                        if (dec) {
                              changeQuantity(dec.dataset.dec, -1);
                        }

                        var remove = event.target.closest("[data-remove]");
                        if (remove) {
                              delete cart[remove.dataset.remove];
                              save(KEYS.cart, cart);
                              renderCart();
                        }

                        var browse = event.target.closest("[data-browse-menu]");
                        if (browse) {
                              toggleDrawer(false);
                              var menuSection = $("menu-section");
                              if (menuSection) menuSection.scrollIntoView({ behavior: "smooth" });
                        }
                  };
            }

            if ($("clearBtn")) {
                  $("clearBtn").onclick = function () {
                        cart = {};
                        save(KEYS.cart, cart);
                        renderCart();
                  };
            }

            if ($("zone")) {
                  $("zone").onchange = renderCart;
            }

            if ($("pay")) {
                  $("pay").onchange = updateTransferInfo;
            }

            function applySavedAddress() {
                  var select = $("savedAddress");
                  if (!select || !select.value) return;
                  var option = select.options[select.selectedIndex];
                  if (!option) return;
                  if ($("inName") && option.dataset.name) {
                        $("inName").value = option.dataset.name;
                        form.inName = option.dataset.name;
                        if ($("fName")) $("fName").classList.remove("err");
                  }
                  if ($("inPhone") && option.dataset.phone) {
                        $("inPhone").value = option.dataset.phone;
                        form.inPhone = option.dataset.phone;
                        if ($("fPhone")) $("fPhone").classList.remove("err");
                  }
                  if ($("inAddr") && option.dataset.address) {
                        $("inAddr").value = option.dataset.address;
                        form.inAddr = option.dataset.address;
                        if ($("fAddr")) $("fAddr").classList.remove("err");
                  }
                  save(KEYS.form, form);
            }

            if ($("savedAddress")) {
                  $("savedAddress").onchange = applySavedAddress;
                  if (!$("inAddr") || !$("inAddr").value.trim()) {
                        applySavedAddress();
                  }
            }

            if ($("cartToggle")) {
                  $("cartToggle").onclick = function () {
                        toggleDrawer(true);
                  };
            }

            if ($("mobileMenuToggle")) {
                  $("mobileMenuToggle").onclick = function () {
                        var nav = document.querySelector(".header-nav");
                        var isOpen = nav && nav.classList.toggle("mobile-open");
                        $("mobileMenuToggle").setAttribute("aria-expanded", isOpen ? "true" : "false");
                  };
            }

            function closeAccountMenu() {
                  var menu = $("accountMenu");
                  var toggle = $("accountToggle");
                  if (!menu || !toggle) return;
                  menu.hidden = true;
                  toggle.setAttribute("aria-expanded", "false");
            }

            if ($("accountToggle") && $("accountMenu")) {
                  $("accountToggle").onclick = function () {
                        var menu = $("accountMenu");
                        var willOpen = menu.hidden;
                        menu.hidden = !willOpen;
                        $("accountToggle").setAttribute("aria-expanded", willOpen ? "true" : "false");
                        if (willOpen) {
                              var firstItem = menu.querySelector("a, button");
                              if (firstItem) firstItem.focus();
                        }
                  };
                  document.addEventListener("keydown", function (event) {
                        if (event.key === "Escape") closeAccountMenu();
                  });
                  document.addEventListener("click", function (event) {
                        var wrap = document.querySelector(".account-menu-wrap");
                        if (wrap && !wrap.contains(event.target)) closeAccountMenu();
                  });
            }

            document.querySelectorAll(".header-nav a").forEach(function (link) {
                  link.addEventListener("click", function () {
                        var nav = document.querySelector(".header-nav");
                        if (nav) nav.classList.remove("mobile-open");
                        if ($("mobileMenuToggle")) $("mobileMenuToggle").setAttribute("aria-expanded", "false");
                  });
            });

            document.addEventListener("click", function (event) {
                  var nav = document.querySelector(".header-nav");
                  var toggle = $("mobileMenuToggle");
                  if (!nav || !toggle || !nav.classList.contains("mobile-open")) return;
                  if (!nav.contains(event.target) && !toggle.contains(event.target)) {
                        nav.classList.remove("mobile-open");
                        toggle.setAttribute("aria-expanded", "false");
                  }
            });

            if ($("mobileCartBtn")) {
                  $("mobileCartBtn").onclick = function () {
                        toggleDrawer(true);
                  };
            }

            if ($("cartClose")) {
                  $("cartClose").onclick = function () {
                        toggleDrawer(false);
                  };
            }

            if ($("scrim")) {
                  $("scrim").onclick = function () {
                        toggleDrawer(false);
                  };
            }

            if ($("orderBtn")) {
                  $("orderBtn").onclick = placeOrder;
            }

            if ($("transferConfirm")) {
                  $("transferConfirm").onclick = function () {
                        transferConfirmed = true;
                        closeTransferModal();
                        placeOrder();
                  };
            }

            [$("transferClose"), $("transferCancel")].forEach(function (button) {
                  if (button) {
                        button.onclick = function () {
                              transferConfirmed = false;
                              closeTransferModal();
                        };
                  }
            });

            if ($("transferModal")) {
                  $("transferModal").onclick = function (event) {
                        if (event.target === $("transferModal")) {
                              transferConfirmed = false;
                              closeTransferModal();
                        }
                  };
            }

            if ($("modal")) {
                  $("modal").onclick = function (event) {
                        if (event.target === $("modal")) {
                              $("modal").classList.remove("open");
                        }
                  };
            }

            document.querySelectorAll(".bottom-nav-item").forEach(function (link) {
                  link.addEventListener("click", function () {
                        if (link.tagName === "A" && (link.getAttribute("href") || "").startsWith("#")) {
                              document.querySelectorAll(".bottom-nav-item").forEach(function (item) {
                                    item.classList.remove("active");
                              });
                              link.classList.add("active");
                        }
                  });
            });

            ["inName", "inPhone", "inAddr"].forEach(function (id) {
                  if ($(id)) {
                        $(id).oninput = function () {
                              form[id] = $(id).value;
                              save(KEYS.form, form);
                        };
                  }
            });
      }

      // ---------- Initialize ----------
      function initialize() {
            var theme = load(KEYS.theme, null);

            if (theme) {
                  document.documentElement.setAttribute("data-theme", theme);
            }

            ["inName", "inPhone", "inAddr"].forEach(function (id) {
                  if (form[id] && $(id)) {
                        $(id).value = form[id];
                  } else if ($(id) && $(id).value) {
                        form[id] = $(id).value;
                  }
            });

            bindEvents();
            updateTransferInfo();
            renderCart();
            loadMenu();

            if (new URLSearchParams(window.location.search).get("open_cart") === "1") {
                  toggleDrawer(true);
            }
      }

      initialize();
})();
