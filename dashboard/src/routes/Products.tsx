import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ItemType, Product, ProductInput } from "../lib/api";
import { api } from "../lib/api";
import { newProductId } from "../lib/id";
import { AlertTriangleIcon, ImageIcon, PackageIcon, XCircleIcon } from "../components/ui/icons";
import { useToast } from "../components/Toast";
import { Modal } from "../components/Modal";
import { formatMoney } from "../lib/format";
import { NumberInput } from "../components/NumberInput";
import { missingProductFields } from "../lib/productForm";

function size(bytes?: number): string {
  if (!bytes) return "";
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

// Shared chrome for the two catalogue-wide warning banners below (missing
// media files, invalid product directories) - same header/toggle/list shape,
// only the title and rows differ.
function MediaIssueBanner({
  title, count, expanded, onToggle, children,
}: {
  title: string; count: number; expanded: boolean; onToggle: () => void; children: React.ReactNode;
}) {
  if (count === 0) return null;
  return (
    <div className="banner warn">
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
        <strong>⚠ {title} ({count})</strong>
        <button onClick={onToggle}>{expanded ? "Ẩn chi tiết" : "Xem chi tiết"}</button>
      </div>
      {expanded && <ul style={{ margin: "8px 0 0", paddingLeft: 20 }}>{children}</ul>}
    </div>
  );
}

function isCombo(product: Product): boolean {
  return (product.item_type ?? "single") === "combo";
}

/** At or below the threshold the shop set for this product - the point of
 * setting one is to be told before it runs out, not after. */
function isLowStock(product: Product): boolean {
  const threshold = product.low_stock_threshold;
  if (threshold == null || product.stock == null) return false;
  return product.stock <= threshold;
}

/**
 * The red stock warning a product needs, if any: out of stock whenever the
 * count is 0 (a threshold or not), otherwise "low" once it is at or under
 * the threshold the shop set.
 */
function stockAlert(product: Product): { level: "out" | "low"; text: string } | null {
  if (product.stock === 0) return { level: "out", text: "Hết hàng" };
  if (!isLowStock(product)) return null;
  return {
    level: "low",
    text: `Sắp hết hàng · còn ${product.stock} (ngưỡng ${product.low_stock_threshold})`,
  };
}

function StockAlert({ product }: { product: Product }) {
  const alert = stockAlert(product);
  if (!alert) return null;
  return (
    <div className={`stock-alert is-${alert.level}`} role="status">
      <AlertTriangleIcon width={14} height={14} aria-hidden="true" />
      {alert.text}
    </div>
  );
}

function splitList(value: string): string[] {
  return value
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
}

/** Currencies the shop actually prices in. Free text would let a typo
 * ("vnd " / "VNĐ") split one currency into several that never compare. */
export const CURRENCIES = [
  { code: "VND", label: "VND — Việt Nam Đồng" },
  { code: "MMK", label: "MMK — Kyat Myanmar" },
  { code: "USD", label: "USD — Đô la Mỹ" },
  { code: "THB", label: "THB — Baht Thái" },
];

type ComboRow = {
  productId: string;
  quantity: string;
};

/** One file picked in the create form, waiting for the product it belongs to
 * to exist on the server. */
type StagedMedia = {
  key: string;
  file: File;
  type: "image" | "video";
  url: string;
};

type FormState = {
  productId: string;
  itemType: ItemType;
  name: string;
  brand: string;
  category: string;
  form: string;
  package: string;
  volume: string;
  description: string;
  price: string;
  costPrice: string;
  currency: string;
  shelfLifeDays: string;
  isActive: boolean;
  stock: string;
  lowStockThreshold: string;
  comboItems: ComboRow[];
  ingredients: string;
  benefits: string;
  warnings: string;
  keywords: string;
  tags: string;
};

const EMPTY_COMBO_ROW: ComboRow = { productId: "", quantity: "" };

const EMPTY_FORM: FormState = {
  productId: "", itemType: "single", name: "", brand: "", category: "",
  form: "", package: "", volume: "", description: "",
  price: "", costPrice: "", currency: "VND", shelfLifeDays: "", isActive: true,
  stock: "", lowStockThreshold: "", comboItems: [{ ...EMPTY_COMBO_ROW }],
  ingredients: "", benefits: "", warnings: "", keywords: "", tags: "",
};

function productToForm(product: Product): FormState {
  const comboItems = (product.combo_items ?? []).map((item) => ({
    productId: item.product_id,
    quantity: String(item.quantity),
  }));
  return {
    productId: product.id,
    itemType: product.item_type ?? "single",
    name: product.name ?? "",
    brand: product.brand ?? "",
    category: product.category ?? "",
    form: product.form ?? "",
    package: product.package ?? "",
    volume: product.volume ?? "",
    description: product.description ?? "",
    price: product.price != null ? String(product.price) : "",
    costPrice: product.cost_price != null ? String(product.cost_price) : "",
    currency: product.currency || "VND",
    shelfLifeDays: product.shelf_life_days != null ? String(product.shelf_life_days) : "",
    isActive: product.is_active !== false,
    // A combo's stock is derived, so there is nothing here to edit - the
    // form shows the resolved count read-only instead.
    stock: product.stock != null ? String(product.stock) : "",
    lowStockThreshold:
      product.low_stock_threshold != null ? String(product.low_stock_threshold) : "",
    comboItems: comboItems.length ? [...comboItems, { ...EMPTY_COMBO_ROW }] : [{ ...EMPTY_COMBO_ROW }],
    ingredients: (product.ingredients ?? []).join(", "),
    benefits: (product.benefits ?? []).join(", "),
    warnings: (product.warnings ?? []).join(", "),
    keywords: (product.keywords ?? []).join(", "),
    tags: (product.tags ?? []).join(", "),
  };
}

function formToFields(form: FormState): ProductInput {
  const isCombo = form.itemType === "combo";
  return {
    name: form.name.trim(),
    item_type: form.itemType,
    brand: form.brand.trim(),
    category: form.category.trim(),
    form: form.form.trim(),
    package: form.package.trim(),
    volume: form.volume.trim(),
    description: form.description.trim(),
    price: form.price.trim() ? Number(form.price) : null,
    cost_price: form.costPrice.trim() ? Number(form.costPrice) : null,
    currency: form.currency,
    shelf_life_days: form.shelfLifeDays.trim() ? Number(form.shelfLifeDays) : null,
    is_active: form.isActive,
    // Sending a stock number for a combo would write a figure the reader
    // then ignores in favour of the derived one - two answers to the same
    // question, one of them stale from the moment it is saved.
    stock: isCombo ? null : form.stock.trim() ? Number(form.stock) : null,
    low_stock_threshold: form.lowStockThreshold.trim()
      ? Number(form.lowStockThreshold)
      : null,
    combo_items: isCombo
      ? form.comboItems
          .filter((row) => row.productId.trim() && row.quantity.trim())
          .map((row) => ({
            product_id: row.productId.trim(),
            quantity: Number(row.quantity),
          }))
      : [],
    ingredients: splitList(form.ingredients),
    benefits: splitList(form.benefits),
    warnings: splitList(form.warnings),
    keywords: splitList(form.keywords),
    tags: splitList(form.tags),
  };
}

// Counts and amounts with no meaning below zero - the backend rejects them
// too (dashboard_api/main.py:_validate_non_negative); checking here as well
// points at the exact field instead of a banner at the top of a long form.
// Only the fields the form shows: a hidden one the operator cannot see or
// fix must not be what blocks "Lưu".
type NumberField = "price" | "costPrice" | "stock" | "lowStockThreshold";
const NON_NEGATIVE_FIELDS: NumberField[] = ["price", "costPrice", "stock", "lowStockThreshold"];

function numberFieldErrors(form: FormState): Partial<Record<NumberField, string>> {
  const errors: Partial<Record<NumberField, string>> = {};
  for (const key of NON_NEGATIVE_FIELDS) {
    // A combo's stock is derived and never sent (see formToFields).
    if (key === "stock" && form.itemType === "combo") continue;
    const raw = form[key].trim();
    if (!raw) continue;
    const value = Number(raw);
    if (!Number.isFinite(value)) errors[key] = "Giá trị phải là một số.";
    else if (value < 0) errors[key] = "Không được nhỏ hơn 0, vui lòng nhập lại.";
  }
  return errors;
}

function FieldError({ message }: { message?: string }) {
  return message ? <small className="product-form-error" role="alert">{message}</small> : null;
}

/**
 * The card's picture: the product's first image that is actually on disk, or
 * a plain placeholder - so every card in the grid has the same shape and a
 * product with no photo yet stands out as one to fix. Opens the details.
 */
function ProductCardImage({
  accountId, product, onOpen,
}: {
  accountId: string;
  product: Product;
  onOpen: () => void;
}) {
  const [failed, setFailed] = useState(false);
  const image = product.media.images.find(
    (file) => file.relative_path && file.exists !== false && file.valid_type !== false,
  );
  const imageCount = product.media.images.length;
  const videoCount = product.media.videos.length;
  const counts = [
    imageCount ? `${imageCount} ảnh` : "",
    videoCount ? `${videoCount} video` : "",
  ].filter(Boolean).join(" · ");
  return (
    <button
      type="button"
      className="product-card-image"
      onClick={onOpen}
      aria-label={`Xem chi tiết ${product.name}`}
    >
      {image && !failed ? (
        <img
          src={api.productMediaUrl(accountId, product.id, image.relative_path!)}
          alt=""
          loading="lazy"
          onError={() => setFailed(true)}
        />
      ) : (
        <span className="product-card-image-empty">
          <ImageIcon />
          {failed ? "Không tải được ảnh" : "Chưa có ảnh"}
        </span>
      )}
      {counts && <span className="product-card-media-count">{counts}</span>}
    </button>
  );
}

function MediaThumb({
  accountId, productId, file, onDelete, deleting, previewUrl,
}: {
  accountId: string;
  productId: string;
  file: Product["media"]["images"][number];
  onDelete: () => void;
  deleting: boolean;
  /** A local object URL for a file chosen but not uploaded yet - a new
   * product has no directory on the server to serve it from. */
  previewUrl?: string;
}) {
  const url = previewUrl
    ?? (file.relative_path
      ? api.productMediaUrl(accountId, productId, file.relative_path)
      : undefined);
  return (
    <div className="media-thumb">
      {file.type === "video" ? (
        <video src={url} muted className="media-thumb-preview" />
      ) : (
        <img src={url} alt={file.name ?? ""} className="media-thumb-preview" />
      )}
      <button
        className="media-thumb-remove"
        title="Xoá file này"
        disabled={deleting}
        onClick={onDelete}
      >
        ✕
      </button>
      <span className="media-thumb-size">{size(file.size)}</span>
      {!file.exists && <span className="media-thumb-missing">thiếu file</span>}
    </div>
  );
}

// Read-only, opposite number to ProductFormModal below - the card now only
// shows a two-line description snippet plus name/price/stock, so anything
// dropped from the card (full description, media, ingredients/benefits/tags,
// combo composition, warnings) lives here instead of being lost.
function ProductDetailModal({
  accountId, product, onClose, onEdit,
}: {
  accountId: string;
  product: Product;
  onClose: () => void;
  onEdit: () => void;
}) {
  const media = [...product.media.images, ...product.media.videos];
  const tagLists: { label: string; values?: string[] }[] = [
    { label: "Thành phần", values: product.ingredients },
    { label: "Công dụng", values: product.benefits },
    { label: "Từ khoá tìm kiếm", values: product.keywords },
    { label: "Thẻ", values: product.tags },
  ];

  return (
    <Modal onClose={onClose} title={`Chi tiết ${product.name}`} style={{ width: "min(620px, 100%)" }}>
      <div className="product-form-header">
        <h3>{product.name}</h3>
        <button onClick={onClose} aria-label="Đóng">×</button>
      </div>

      <p className="sub" style={{ marginTop: -8 }}>
        <code>{product.id}</code>
        {[product.brand, product.category, product.form, product.package, product.volume]
          .filter(Boolean)
          .map((part) => ` · ${part}`)}
        {product.is_active === false && <span className="tag bad" style={{ marginLeft: 8 }}>Ngừng bán</span>}
      </p>

      {product.description && <p style={{ whiteSpace: "pre-wrap" }}>{product.description}</p>}

      <div className="kv">
        <span className="k">Giá</span>
        <span className="v">{formatMoney(product.price, { currency: product.currency ?? "₫", whenNull: "chưa có" })}</span>
      </div>
      {product.cost_price != null && (
        <div className="kv">
          <span className="k">Giá vốn</span>
          <span className="v">{formatMoney(product.cost_price, { currency: product.currency ?? "₫" })}</span>
        </div>
      )}
      <StockAlert product={product} />
      <div className="kv">
        <span className="k">{isCombo(product) ? "Còn bán được" : "Tồn kho"}</span>
        <span className={`v${stockAlert(product) ? " stock-value-alert" : ""}`}>
          {product.stock ?? "chưa có"}
          {isCombo(product) && <span className="product-card-note"> combo</span>}
        </span>
      </div>
      {product.shelf_life_days != null && (
        <div className="kv">
          <span className="k">Hạn sử dụng</span>
          <span className="v">{product.shelf_life_days} ngày kể từ ngày sản xuất</span>
        </div>
      )}

      {isCombo(product) && (product.combo_items?.length ?? 0) > 0 && (
        <>
          <h4 style={{ marginBottom: 6 }}>Gồm những sản phẩm</h4>
          {product.combo_items!.map((item) => (
            <div className="kv" key={item.product_id}>
              <span className="k"><code>{item.product_id}</code></span>
              <span className="v">× {item.quantity}</span>
            </div>
          ))}
        </>
      )}

      {tagLists.map(({ label, values }) =>
        values && values.length > 0 ? (
          <div key={label} style={{ marginTop: 10 }}>
            <div className="detail-section-label" style={{ marginBottom: 6 }}>{label}</div>
            <div className="quick-reply-list">
              {values.map((value) => <span className="tag" key={value}>{value}</span>)}
            </div>
          </div>
        ) : null,
      )}

      {(product.warnings?.length ?? 0) > 0 && (
        <div style={{ marginTop: 10 }}>
          <div className="detail-section-label" style={{ marginBottom: 6, color: "var(--red)" }}>Cảnh báo</div>
          {product.warnings!.map((warning) => (
            <div key={warning} style={{ fontSize: 12.5, color: "var(--red)" }}>⚠ {warning}</div>
          ))}
        </div>
      )}

      {media.length > 0 && (
        <div style={{ marginTop: 14 }}>
          <div className="detail-section-label" style={{ marginBottom: 6 }}>Hình ảnh / video</div>
          <div className="media-thumb-grid">
            {media.map((file, index) => (
              <div className="media-thumb media-thumb-readonly" key={`${file.relative_path}-${index}`}>
                {file.type === "video" ? (
                  <video
                    src={file.relative_path ? api.productMediaUrl(accountId, product.id, file.relative_path) : undefined}
                    muted
                    className="media-thumb-preview"
                  />
                ) : (
                  <img
                    src={file.relative_path ? api.productMediaUrl(accountId, product.id, file.relative_path) : undefined}
                    alt={file.name ?? ""}
                    className="media-thumb-preview"
                  />
                )}
              </div>
            ))}
          </div>
        </div>
      )}

      <div className="product-card-actions">
        <button className="primary" onClick={onEdit}>Sửa sản phẩm</button>
      </div>
    </Modal>
  );
}

function ProductFormModal({
  accountId,
  editingId,
  initial,
  onClose,
  onCreated,
}: {
  accountId: string;
  editingId: string | null;
  initial: Product | null;
  onClose: () => void;
  /** Hands the just-created product back so the form can continue as an
   * edit - used when its media could not all be attached. */
  onCreated: (product: Product) => void;
}) {
  const client = useQueryClient();
  const toast = useToast();
  // Same query key as the parent's catalogue list, so this shares its cache -
  // an upload/delete here invalidates that one key and both re-render with
  // the fresh media list, instead of the modal being stuck with the product
  // snapshot from the moment "Sửa" was clicked.
  const catalogue = useQuery({
    queryKey: ["products", accountId],
    queryFn: () => api.products(accountId),
  });
  const editing = editingId
    ? catalogue.data?.products.find((item) => item.id === editingId) ?? initial
    : null;

  // A new product gets a random id up front rather than one typed in or
  // derived from its name: nobody has to invent a code, and renaming the
  // product never makes the id look wrong. Picked once, so a retried create
  // after an error reuses it instead of minting another.
  const [form, setForm] = useState<FormState>(() =>
    initial ? productToForm(initial) : { ...EMPTY_FORM, productId: newProductId() },
  );
  const [error, setError] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [mediaType, setMediaType] = useState<"image" | "video">("image");
  // A product that does not exist yet has no directory to upload into, so
  // files chosen while creating one are held here and sent once it does.
  const [staged, setStaged] = useState<StagedMedia[]>([]);

  // Object URLs are held by the browser until revoked, and the modal closing
  // is the last moment anything can still be previewing them. Read through a
  // ref so this runs on unmount only: depending on `staged` would revoke the
  // previous list every time a file is added, blanking the thumbnails of the
  // files already chosen.
  const stagedRef = useRef<StagedMedia[]>([]);
  stagedRef.current = staged;
  useEffect(() => () => {
    stagedRef.current.forEach((item) => URL.revokeObjectURL(item.url));
  }, []);

  const stageFile = (file: File) =>
    setStaged((prev) => [
      ...prev,
      { key: `${file.name}-${Date.now()}`, file, type: mediaType, url: URL.createObjectURL(file) },
    ]);

  const unstage = (key: string) =>
    setStaged((prev) => {
      const item = prev.find((entry) => entry.key === key);
      if (item) URL.revokeObjectURL(item.url);
      return prev.filter((entry) => entry.key !== key);
    });

  const invalidate = () => client.invalidateQueries({ queryKey: ["products", accountId] });

  // Only single items can be components, and a combo can never contain
  // itself - the same rule the API enforces, applied here so an impossible
  // choice is never offered in the first place.
  const singles = (catalogue.data?.products ?? []).filter(
    (item) => (item.item_type ?? "single") === "single" && item.id !== editingId,
  );

  const setComboRow = (index: number, patch: Partial<ComboRow>) =>
    setForm((prev) => ({
      ...prev,
      comboItems: prev.comboItems.map((row, position) =>
        position === index ? { ...row, ...patch } : row,
      ),
    }));

  const addComboRow = () =>
    setForm((prev) => ({ ...prev, comboItems: [...prev.comboItems, { ...EMPTY_COMBO_ROW }] }));

  const removeComboRow = (index: number) =>
    setForm((prev) => {
      const remaining = prev.comboItems.filter((_, position) => position !== index);
      // Never leave the section with no row at all: an empty list offers no
      // way back to adding one except the button below it.
      return { ...prev, comboItems: remaining.length ? remaining : [{ ...EMPTY_COMBO_ROW }] };
    });

  // What the backend will derive, shown before saving so the number is not a
  // surprise that only appears after the combo is already on sale.
  const comboSellable = (() => {
    const rows = form.comboItems.filter((row) => row.productId.trim() && Number(row.quantity) > 0);
    if (!rows.length) return "0";
    const counts = rows.map((row) => {
      const component = singles.find((item) => item.id === row.productId);
      if (!component) return 0;
      return Math.floor(Number(component.stock ?? 0) / Number(row.quantity));
    });
    return String(Math.max(0, Math.min(...counts)));
  })();

  const create = useMutation({
    mutationFn: async () => {
      const product = await api.createProduct(accountId, form.productId, formToFields(form));
      // Media can only be attached to a product that exists, so these run
      // after it does. One file failing must not discard the rest, and must
      // not be reported as if the product itself failed - by this point it
      // is already saved.
      const failed: string[] = [];
      for (const item of staged) {
        try {
          await api.uploadProductMedia(accountId, product.id, item.type, item.file);
        } catch {
          failed.push(item.file.name);
        }
      }
      return { product, failed };
    },
    onSuccess: ({ product, failed }) => {
      invalidate();
      if (failed.length) {
        // Closing here would report success while those files quietly went
        // nowhere. Handing the modal over to edit mode for the product that
        // *was* created shows exactly which files made it and offers the
        // ordinary upload button to add the rest.
        setError(`Đã tạo sản phẩm nhưng chưa tải lên được: ${failed.join(", ")}`);
        setStaged([]);
        onCreated(product);
        return;
      }
      onClose();
    },
    onError: (err: Error) => setError(err.message),
  });

  const update = useMutation({
    mutationFn: () => api.updateProduct(accountId, editingId!, formToFields(form)),
    onSuccess: () => {
      invalidate();
      onClose();
    },
    onError: (err: Error) => setError(err.message),
  });

  const upload = useMutation({
    mutationFn: (file: File) =>
      api.uploadProductMedia(accountId, editingId!, mediaType, file),
    onSuccess: invalidate,
    onError: (err: Error) => toast.error("Không tải được file lên", err.message),
  });

  const removeMedia = useMutation({
    mutationFn: (relativePath: string) =>
      api.deleteProductMedia(accountId, editingId!, relativePath),
    onSuccess: invalidate,
    onError: (err: Error) => toast.error("Không xoá được file", err.message),
  });

  const saving = create.isPending || update.isPending;
  const fieldErrors = numberFieldErrors(form);
  const hasFieldErrors = Object.keys(fieldErrors).length > 0;
  const missing = missingProductFields(form);
  const canSubmit = missing.length === 0 && !hasFieldErrors;

  return (
    <Modal onClose={onClose} title={editing ? "Sửa sản phẩm" : "Thêm sản phẩm"}>
        <div className="product-form-header">
          <h3>{editing ? "Sửa sản phẩm" : "Thêm sản phẩm"}</h3>
          <button onClick={onClose}>Đóng</button>
        </div>

        {error && <div className="banner bad">{error}</div>}

  
        {/* Brand, packaging, ingredients, keywords and shelf life are no
            longer on the form. An existing product keeps whatever it already
            had in them:
            productToForm reads them and formToFields sends them back as is. */}
        <div className="product-form-grid">
          <label className="product-form-field">
            <span>Loại sản phẩm</span>
            <select
              value={form.itemType}
              disabled={Boolean(editing)}
              onChange={(event) =>
                setForm((prev) => ({ ...prev, itemType: event.target.value as ItemType }))
              }
            >
              <option value="single">Sản phẩm lẻ</option>
              <option value="combo">Combo</option>
            </select>
            <small className="product-form-hint">
              {editing
                ? "Loại mặt hàng được khoá sau khi tạo để không làm sai lịch sử kho."
                : "Combo được bán như một mặt hàng nhưng trừ kho theo từng sản phẩm lẻ."}
            </small>
          </label>
          <label className="product-form-field">
            <span className="is-required">Tên sản phẩm</span>
            <input aria-required="true"
              value={form.name}
              onChange={(event) => setForm((prev) => ({ ...prev, name: event.target.value }))}
            />
          </label>
          <label className="product-form-field">
            <span className="is-required">Danh mục</span>
            <input aria-required="true" value={form.category} onChange={(event) => setForm((prev) => ({ ...prev, category: event.target.value }))} />
          </label>
          <label className="product-form-field">
            <span className="is-required">Đơn giá</span>
            <NumberInput aria-required="true"
              decimal
              value={form.price}
              aria-invalid={Boolean(fieldErrors.price)}
              onValueChange={(price) => setForm((prev) => ({ ...prev, price }))}
            />
            <FieldError message={fieldErrors.price} />
          </label>
          <label className="product-form-field">
            <span>Giá vốn</span>
            <NumberInput
              decimal
              value={form.costPrice}
              aria-invalid={Boolean(fieldErrors.costPrice)}
              onValueChange={(costPrice) => setForm((prev) => ({ ...prev, costPrice }))}
            />
            <FieldError message={fieldErrors.costPrice} />
          </label>
          <label className="product-form-field">
            <span>Đơn vị tiền tệ</span>
            <select
              value={form.currency}
              onChange={(event) => setForm((prev) => ({ ...prev, currency: event.target.value }))}
            >
              {CURRENCIES.map((item) => (
                <option key={item.code} value={item.code}>{item.label}</option>
              ))}
            </select>
          </label>
          {form.itemType === "combo" ? (
            <label className="product-form-field">
              <span>Tồn kho (số combo còn bán được)</span>
              <input value={comboSellable} disabled />
              <small className="product-form-hint">
                Tính từ tồn kho của các sản phẩm lẻ, không nhập tay.
              </small>
            </label>
          ) : (
            <label className="product-form-field">
              <span className="is-required">Tồn kho</span>
              <NumberInput aria-required="true"
                value={form.stock}
                aria-invalid={Boolean(fieldErrors.stock)}
                onValueChange={(stock) => setForm((prev) => ({ ...prev, stock }))}
              />
              <FieldError message={fieldErrors.stock} />
            </label>
          )}
          <label className="product-form-field">
            <span>Ngưỡng cảnh báo sắp hết</span>
            <NumberInput
              value={form.lowStockThreshold}
              aria-invalid={Boolean(fieldErrors.lowStockThreshold)}
              onValueChange={(lowStockThreshold) =>
                setForm((prev) => ({ ...prev, lowStockThreshold }))
              }
            />
            <FieldError message={fieldErrors.lowStockThreshold} />
            <small className="product-form-hint">Tồn kho bằng hoặc dưới mức này thì báo sắp hết.</small>
          </label>
          <label className="product-form-field product-form-field-check">
            <input
              type="checkbox"
              checked={form.isActive}
              onChange={(event) => setForm((prev) => ({ ...prev, isActive: event.target.checked }))}
            />
            <span>Đang sử dụng</span>
            <small className="product-form-hint">
              Bỏ chọn để ngừng bán: agent sẽ không còn tư vấn mặt hàng này cho khách.
            </small>
          </label>
        </div>

        <label className="product-form-field product-form-field-wide">
          <span>Mô tả</span>
          <textarea
            rows={3}
            value={form.description}
            onChange={(event) => setForm((prev) => ({ ...prev, description: event.target.value }))}
          />
        </label>

        {form.itemType === "combo" && (
          <div className="product-form-section">
            <div className="product-form-section-head">
              <div className="customer-info-section-title">Thành phần combo</div>
              <span className="product-form-badge">TRỪ KHO THEO ĐỊNH MỨC</span>
            </div>
            {form.comboItems.map((row, index) => (
              <div className="product-form-combo-row" key={index}>
                <label className="product-form-field">
                  <span>Sản phẩm lẻ</span>
                  <select
                    value={row.productId}
                    onChange={(event) => setComboRow(index, { productId: event.target.value })}
                  >
                    <option value="">---------</option>
                    {singles.map((item) => (
                      <option key={item.id} value={item.id}>
                        {item.id} · {item.name} · còn {item.stock ?? 0}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="product-form-field product-form-field-narrow">
                  <span>Số lượng trong 1 combo</span>
                  <NumberInput
                    value={row.quantity}
                    onValueChange={(quantity) => setComboRow(index, { quantity })}
                  />
                </label>
                <button
                  type="button"
                  className="product-form-combo-remove"
                  title="Bỏ thành phần này"
                  onClick={() => removeComboRow(index)}
                >
                  ✕
                </button>
              </div>
            ))}
            <button type="button" className="ghost" onClick={addComboRow}>
              + Thêm sản phẩm lẻ
            </button>
          </div>
        )}

        <div className="product-form-media">
          <div className="customer-info-section-title">Ảnh &amp; video</div>
          <div className="media-thumb-grid">
            {editing
              ? [...editing.media.images, ...editing.media.videos].map((file, index) => (
                  <MediaThumb
                    key={`${file.relative_path}-${index}`}
                    accountId={accountId}
                    productId={editing.id}
                    file={file}
                    deleting={removeMedia.isPending}
                    onDelete={() => file.relative_path && removeMedia.mutate(file.relative_path)}
                  />
                ))
              : staged.map((item) => (
                  <MediaThumb
                    key={item.key}
                    accountId={accountId}
                    productId={form.productId}
                    file={{
                      type: item.type,
                      name: item.file.name,
                      size: item.file.size,
                      exists: true,
                      relative_path: "",
                    }}
                    previewUrl={item.url}
                    deleting={create.isPending}
                    onDelete={() => unstage(item.key)}
                  />
                ))}
          </div>
          <div className="product-form-upload">
            <select value={mediaType} onChange={(event) => setMediaType(event.target.value as "image" | "video")}>
              <option value="image">Ảnh</option>
              <option value="video">Video</option>
            </select>
            <button
              disabled={upload.isPending || create.isPending}
              onClick={() => fileInputRef.current?.click()}
            >
              {upload.isPending ? "Đang tải lên..." : "+ Tải file lên"}
            </button>
            <input
              ref={fileInputRef}
              type="file"
              accept={mediaType === "image" ? "image/*" : "video/*"}
              style={{ display: "none" }}
              onChange={(event) => {
                const file = event.target.files?.[0];
                // Before the product exists there is nowhere to upload to,
                // so the file waits here and goes up with "Tạo sản phẩm".
                if (file) (editing ? upload.mutate(file) : stageFile(file));
                event.target.value = "";
              }}
            />
            {!editing && staged.length > 0 && (
              <span className="product-form-hint">
                {staged.length} file sẽ được tải lên khi bấm "Tạo sản phẩm".
              </span>
            )}
          </div>
        </div>

        <div className="product-form-footer">
          {missing.length > 0 && (
            <span className="hint form-missing" role="status">
              Còn thiếu: {missing.join(", ")}
            </span>
          )}
          <button onClick={onClose}>Huỷ</button>
          <button
            className="primary"
            disabled={!canSubmit || saving}
            onClick={() => (editing ? update.mutate() : create.mutate())}
          >
            {editing
              ? "Lưu thay đổi"
              : create.isPending && staged.length
                ? "Đang tải file lên..."
                : "Tạo sản phẩm"}
          </button>
        </div>
    </Modal>
  );
}

export function Products({
  accountId, focusId, onFocused,
}: {
  accountId: string;
  // Set by the ⌘K palette to open one product's form straight away - applied
  // once the catalogue has loaded, then cleared via onFocused so it doesn't
  // re-fire on the next refetch (same shape as Customers.focusConversationId).
  focusId?: string | null;
  onFocused?: () => void;
}) {
  const client = useQueryClient();
  const toast = useToast();
  const [formOpen, setFormOpen] = useState<"create" | Product | null>(null);
  const [detailId, setDetailId] = useState<string | null>(null);
  const [showMediaIssues, setShowMediaIssues] = useState(false);

  const catalogue = useQuery({
    queryKey: ["products", accountId],
    queryFn: () => api.products(accountId),
  });

  const remove = useMutation({
    mutationFn: (productId: string) => api.deleteProduct(accountId, productId),
    onSuccess: () => client.invalidateQueries({ queryKey: ["products", accountId] }),
    onError: (err: Error) => toast.error("Không xoá được sản phẩm", err.message),
  });

  const setDefault = useMutation({
    mutationFn: (productId: string | null) => api.saveDefaultProduct(accountId, productId),
    onSuccess: () => client.invalidateQueries({ queryKey: ["products", accountId] }),
    onError: (err: Error) => toast.error("Không đặt được sản phẩm mặc định", err.message),
  });
  const chosenDefault = catalogue.data?.default_product_id ?? null;
  const effectiveDefault = catalogue.data?.effective_default_product_id ?? null;
  const effectiveDefaultName = catalogue.data?.products.find((p) => p.id === effectiveDefault)?.name;

  useEffect(() => {
    if (focusId == null) return;
    const target = catalogue.data?.products.find((p) => p.id === focusId);
    if (target) {
      setFormOpen(target);
      onFocused?.();
      return;
    }
    // Not in the (possibly stale) cached catalogue yet - force a fresh fetch
    // before giving up, so a just-created/edited product found via ⌘K
    // search still opens instead of being silently dropped.
    let cancelled = false;
    catalogue.refetch().then((result) => {
      if (cancelled) return;
      const fresh = result.data?.products.find((p) => p.id === focusId);
      if (fresh) setFormOpen(fresh);
      onFocused?.();
    });
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focusId]);

  // Re-derived from the live catalogue every render (not the object captured
  // when the dialog opened) so an edit made from inside it is reflected
  // immediately, same reasoning as UsersAdmin's detailUser/editUser.
  const detailProduct = detailId == null
    ? null
    : catalogue.data?.products.find((item) => item.id === detailId) ?? null;

  const totalProducts = catalogue.data?.products.length ?? 0;
  const outOfStock = catalogue.data?.products.filter((p) => p.stock === 0).length ?? 0;
  // Already counted as out of stock - listing them twice would read as more
  // products needing attention than there are.
  const lowStock =
    catalogue.data?.products.filter((p) => p.stock !== 0 && isLowStock(p)).length ?? 0;
  const mediaIssues =
    (catalogue.data?.missing_media.length ?? 0) + (catalogue.data?.invalid_product_directories.length ?? 0);

  return (
    <div className="main">
      <header style={{ display: "flex", alignItems: "baseline", justifyContent: "space-between" }}>
        <h2>Sản phẩm &amp; Media</h2>
        <button className="primary" onClick={() => setFormOpen("create")}>
          + Thêm sản phẩm
        </button>
      </header>
      <p className="sub">
        Danh mục sản phẩm riêng của workspace này - thêm, sửa, xoá và quản lý ảnh/video
        trực tiếp tại đây.
      </p>

      {totalProducts > 0 && (
        <div className="stat-cards" style={{ marginBottom: 20 }}>
          <div className="stat-card tint-brand">
            <PackageIcon className="stat-card-decor" width={64} height={64} />
            <div className="stat-card-top">
              <span className="stat-card-eyebrow">Tổng sản phẩm</span>
              <span className="stat-card-icon brand"><PackageIcon width={18} height={18} /></span>
            </div>
            <div className="n">{totalProducts}</div>
            <div className="t">Trong danh mục workspace này</div>
          </div>
          <div className={`stat-card ${outOfStock > 0 ? "tint-danger" : "tint-warn"}`}>
            <AlertTriangleIcon className="stat-card-decor" width={64} height={64} />
            <div className="stat-card-top">
              <span className="stat-card-eyebrow">Hết hàng</span>
              <span className={`stat-card-icon ${outOfStock > 0 ? "danger" : "warn"}`}><AlertTriangleIcon width={18} height={18} /></span>
            </div>
            <div className="n">{outOfStock}</div>
            <div className="t">Sản phẩm còn 0 tồn kho</div>
          </div>
          {lowStock > 0 && (
            <div className="stat-card tint-danger">
              <AlertTriangleIcon className="stat-card-decor" width={64} height={64} />
              <div className="stat-card-top">
                <span className="stat-card-eyebrow">Sắp hết</span>
                <span className="stat-card-icon danger"><AlertTriangleIcon width={18} height={18} /></span>
              </div>
              <div className="n">{lowStock}</div>
              <div className="t">Đã chạm ngưỡng cảnh báo</div>
            </div>
          )}
          <div className="stat-card tint-danger">
            <XCircleIcon className="stat-card-decor" width={64} height={64} />
            <div className="stat-card-top">
              <span className="stat-card-eyebrow">Vấn đề media</span>
              <span className="stat-card-icon danger"><XCircleIcon width={18} height={18} /></span>
            </div>
            <div className="n">{mediaIssues}</div>
            <div className="t">File thiếu hoặc thư mục lỗi</div>
          </div>
        </div>
      )}


      <MediaIssueBanner
        title="Media lỗi"
        count={catalogue.data?.missing_media.length ?? 0}
        expanded={showMediaIssues}
        onToggle={() => setShowMediaIssues((v) => !v)}
      >
        {catalogue.data?.missing_media.map((file, index) => (
          <li key={`${file.product_id}-${file.path}-${index}`} style={{ fontSize: 12 }}>
            <code>{file.product_id}</code> — thiếu file {file.type}
          </li>
        ))}
      </MediaIssueBanner>

      <MediaIssueBanner
        title="Thư mục sản phẩm không hợp lệ"
        count={catalogue.data?.invalid_product_directories.length ?? 0}
        expanded={showMediaIssues}
        onToggle={() => setShowMediaIssues((v) => !v)}
      >
        {catalogue.data?.invalid_product_directories.map((item) => (
          <li key={item.directory} style={{ fontSize: 12 }}>
            <code>{item.directory}/</code> — thiếu <code>product.json</code>
          </li>
        ))}
      </MediaIssueBanner>

      {totalProducts > 0 && (
        <div className="banner" style={{ marginBottom: 14 }} data-testid="default-product-banner">
          {effectiveDefault ? (
            <>
              Khách hỏi giá/thông tin mà không nói tên sản phẩm → agent hiểu là hỏi{" "}
              <b>{effectiveDefaultName ?? effectiveDefault}</b>
              {chosenDefault === effectiveDefault ? " (đã đặt làm mặc định)." : " (sản phẩm lẻ duy nhất đang bán)."}
            </>
          ) : (
            <>
              Chưa có sản phẩm mặc định: khách hỏi giá mà không nói tên sản phẩm thì agent sẽ hỏi lại
              khách muốn hỏi sản phẩm nào. Bấm "Đặt làm mặc định" trên sản phẩm shop bán.
            </>
          )}
          {chosenDefault && chosenDefault !== effectiveDefault && (
            <div style={{ marginTop: 4 }}>
              Sản phẩm đã chọn <code>{chosenDefault}</code> không còn dùng được (đã xoá, ngừng bán hoặc là combo).
            </div>
          )}
          {chosenDefault && (
            <button
              style={{ marginLeft: 8 }}
              disabled={setDefault.isPending}
              onClick={() => setDefault.mutate(null)}
            >
              Bỏ chọn mặc định
            </button>
          )}
        </div>
      )}

      <div className="grid product-grid">
        {(catalogue.data?.products.length ?? 0) === 0 && (
          <p className="empty">Chưa có sản phẩm nào - bấm "Thêm sản phẩm" để bắt đầu.</p>
        )}
        {catalogue.data?.products.map((product) => {
          const hasExtra = Boolean(
            product.description || product.media.images.length || product.media.videos.length ||
            product.warnings?.length,
          );
          return (
            <section
              className={`panel product-card${stockAlert(product) ? " has-stock-alert" : ""}`}
              key={product.id}
            >
              <StockAlert product={product} />
              <ProductCardImage
                accountId={accountId}
                product={product}
                onOpen={() => setDetailId(product.id)}
              />
              <h3>
                {product.name}
                {product.id === effectiveDefault && (
                  <span className="tag" style={{ marginLeft: 8 }}>Mặc định</span>
                )}
              </h3>
              <p className="sub" style={{ marginTop: -6 }}>
                <code>{product.id}</code>
                {product.category && ` · ${product.category}`}
              </p>

              <p className={`product-card-desc${product.description ? "" : " is-empty"}`}>
                {product.description || "Chưa có mô tả"}
              </p>

              <div className="kv">
                <span className="k">Giá</span>
                <span className="v">
                  {formatMoney(product.price, { currency: product.currency ?? "₫", whenNull: "chưa có" })}
                </span>
              </div>
              <div className="kv">
                <span className="k">{isCombo(product) ? "Còn bán được" : "Tồn kho"}</span>
                <span className={`v${stockAlert(product) ? " stock-value-alert" : ""}`}>
                  {product.stock ?? "chưa có"}
                  {isCombo(product) && <span className="product-card-note"> combo</span>}
                  {(product.warnings?.length ?? 0) > 0 && (
                    <span className="product-card-low" title={product.warnings!.join(", ")}>
                      ⚠ {product.warnings!.length} cảnh báo
                    </span>
                  )}
                </span>
              </div>

              <div className="product-card-actions">
                {hasExtra && (
                  <button onClick={() => setDetailId(product.id)}>Xem chi tiết</button>
                )}
                <button onClick={() => setFormOpen(product)}>Sửa</button>
                {!isCombo(product) && product.is_active !== false && product.id !== chosenDefault && (
                  <button
                    disabled={setDefault.isPending}
                    onClick={() => setDefault.mutate(product.id)}
                  >
                    Đặt làm mặc định
                  </button>
                )}
                <button
                  className="danger"
                  disabled={remove.isPending}
                  onClick={() => {
                    if (window.confirm(`Xoá sản phẩm "${product.name}"?`)) {
                      remove.mutate(product.id);
                    }
                  }}
                >
                  Xoá
                </button>
              </div>
            </section>
          );
        })}
      </div>

      {formOpen && (
        <ProductFormModal
          accountId={accountId}
          editingId={formOpen === "create" ? null : formOpen.id}
          initial={formOpen === "create" ? null : formOpen}
          onClose={() => setFormOpen(null)}
          onCreated={(product) => setFormOpen(product)}
        />
      )}

      {detailProduct && (
        <ProductDetailModal
          accountId={accountId}
          product={detailProduct}
          onClose={() => setDetailId(null)}
          onEdit={() => { setDetailId(null); setFormOpen(detailProduct); }}
        />
      )}
    </div>
  );
}
