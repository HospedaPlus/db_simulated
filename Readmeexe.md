# Solo Neon (Rama: development)

> **⚠️ IMPORTANTE:** Este archivo es de configuración local para el entorno de desarrollo y **nunca** se sube a GitHub. Asegúrate de incluirlo en tu `.gitignore`.

```env
NEON_OWNER_URL=postgresql://hospedaplus_owner:TU_CLAVE@ep-xxxx.c-6.us-east-2.aws.neon.tech/hospedaplus?sslmode=require&channel_binding=require
NEON_APP_URL=postgresql://app_writer:TU_CLAVE@ep-xxxx-pooler.c-6.us-east-2.aws.neon.tech/hospedaplus?sslmode=require
NEON_SEED_MODE=full
SIM_BOOKINGS_PER_MIN=6
```

## 🔌 Configuración de Variables de Entorno

Obtén las dos URLs desde la consola de **Neon**, dentro de la sección **Connect**, utilizando la rama **development** y la base de datos **hospedaplus**:

*   **`NEON_OWNER_URL`**: Rol `hospedaplus_owner`, con el **pooling apagado** (el host va sin `-pooler`).
*   **`NEON_APP_URL`**: Rol `app_writer`, con el **pooling encendido** (el host debe llevar `-pooler`). *Nota: La contraseña la debes escribir tú.*

---

## 🚀 Instrucciones de Ejecución

Ejecuta los siguientes comandos en tu terminal en este orden estricto:

### 1. Verificar la conexión
```bash
docker compose run --rm --build neon-check
```
*   **Nota:** Sirve para comprobar que el entorno apunta a `...neon.tech` y no a `localhost`. **Es normal que falle** en este punto porque todavía no existen las tablas en la base de datos.

### 2. Poblar la base de datos (Seed)
```bash
docker compose run --rm --build neon-seed
```
*   **Nota:** Este comando carga toda la información inicial y debe finalizar correctamente mostrando un estado de **24/24**.

### 3. Iniciar el simulador de reservas
```bash
docker compose up --build neon-simulator
```
*   **Nota:** Comienza a crear reservas continuas en Neon. Para detenerlo, presiona `Ctrl + C`.

---

## 📊 Monitoreo de Reservas

Existen dos formas de verificar que las reservas se están generando correctamente mientras el `neon-simulator` está corriendo:

### Opción 1: Desde la Terminal
En la misma terminal donde encendiste el simulador, verás aparecer líneas de log cada pocos segundos con este formato:

```text
00:25:10 confirmed F3-S5A1B2C3D4E HP-CO-009 DOUBLE 2026-11-08 x2n direct_web
00:25:20 cancelled F3-0104512 900.0 h before check-in, penalty 0.00% = 0.00 COP
```

### Opción 2: Desde la Consola de Neon
Abre el **SQL Editor** en Neon, asegúrate de seleccionar la rama `development` junto a la base de datos `hospedaplus`, y utiliza las siguientes consultas:

*   **Ver las últimas 20 reservas:**
    Las reservas que comienzan con `F3-S` pertenecen al simulador en tiempo real; las demás corresponden al seed inicial. Cada vez que ejecutes el query verás nuevos registros arriba.
    ```sql
    SELECT code, status, check_in, check_out, total_amount, currency, created_at
    FROM operation.reservations
    WHERE code LIKE 'F3-S%'
    ORDER BY created_at DESC
    LIMIT 20;
    ```

*   **Ver el conteo de reservas crecer:**
    Con la configuración actual de 6 reservas por minuto, deberías ver aproximadamente 30 nuevas reservas cada 5 minutos.
    ```sql
    SELECT status, count(*) FROM operation.reservations
    WHERE created_at > now() - interval '15 minutes'
    GROUP BY status;
    ```

> **💡 Soporte:** Si después de unos minutos el conteo sigue en cero y no aparece ninguna reserva nueva, por favor envíame los logs que muestra la terminal de tu simulador para revisar el problema.
