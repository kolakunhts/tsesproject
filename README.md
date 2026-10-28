ການເຮັດວຽກ

## ລັນໂປຣເຈັກ

```bash
source .venv/bin/activate
flask --app app run --debug
```

ເປີດ `http://127.0.0.1:5000`.

## Stripe Test Mode

1. ສ້າງ Stripe account ແລະເປີດ Test mode.
2. ສ້າງ `.env` ຈາກ `.env.example` ແລ້ວໃສ່ `STRIPE_SECRET_KEY`.
3. ລັນ webhook listener ເພື່ອສົ່ງ event ເຂົ້າໂປຣເຈັກ:

```bash
stripe listen --forward-to http://127.0.0.1:5000/api/stripe/webhook
```

ນຳ `whsec_...` ທີ່ Stripe CLI ສະແດງໃຫ້ໄປໃສ່ `STRIPE_WEBHOOK_SECRET`.

ເລືອກ `Stripe Test Card` ໃນກະຕ່າ ແລະໃຊ້ Test Card `4242 4242 4242 4242`, ວັນໝົດອາຍຸໃດກໍໄດ້ໃນອະນາຄົດ ແລະ CVC ໃດກໍໄດ້.

ການສ້າງ Checkout Session ແລະການຮັບ `checkout.session.completed` ອີງຕາມ Stripe Checkout API ແລະ Webhook.

## ລັນດ້ວຍ Docker

ຕ້ອງມີ Docker Desktop ແລະມີ `.env` ຢູ່ໃນໂຟນເດີໂປຣເຈັກ.

```bash
docker compose up --build
```

ເປີດ `http://127.0.0.1:5000`.

ຢຸດ Container:

```bash
docker compose down
```

Database ແລະໄຟລ໌ສະລິບຈະຢູ່ໃນເຄື່ອງຜ່ານ Volume mapping.
