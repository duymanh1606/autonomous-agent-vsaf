// Inline stroke-SVG icons - the project has no icon library dependency yet,
// and the design spec forbids adding one just to swap icons.
import type { ReactNode, SVGProps } from "react";

type IconProps = SVGProps<SVGSVGElement>;

function base(children: ReactNode, props: IconProps) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.8}
      strokeLinecap="round"
      strokeLinejoin="round"
      width={18}
      height={18}
      {...props}
    >
      {children}
    </svg>
  );
}

export const HomeIcon = (props: IconProps) =>
  base(
    <>
      <path d="M3 11.5 12 4l9 7.5" />
      <path d="M5.5 10v9a1 1 0 0 0 1 1H9a1 1 0 0 0 1-1v-4a1 1 0 0 1 1-1h2a1 1 0 0 1 1 1v4a1 1 0 0 0 1 1h2.5a1 1 0 0 0 1-1v-9" />
    </>,
    props,
  );

export const InboxIcon = (props: IconProps) =>
  base(
    <>
      <path d="M4 12h4l1.5 3h5L16 12h4" />
      <path d="M5.2 6.4 4 12v6a1.5 1.5 0 0 0 1.5 1.5h13A1.5 1.5 0 0 0 20 18v-6l-1.2-5.6A1.5 1.5 0 0 0 17.34 5H6.66a1.5 1.5 0 0 0-1.46 1.4Z" />
    </>,
    props,
  );

export const PackageIcon = (props: IconProps) =>
  base(
    <>
      <path d="m3.5 7.5 8.5-4 8.5 4-8.5 4-8.5-4Z" />
      <path d="M3.5 7.5v9l8.5 4 8.5-4v-9" />
      <path d="M12 11.5v9" />
    </>,
    props,
  );

export const SlidersIcon = (props: IconProps) =>
  base(
    <>
      <path d="M5 6h9M17 6h2M5 12h2M10 12h9M5 18h13M18 18h1" />
      <circle cx="14" cy="6" r="2" />
      <circle cx="7" cy="12" r="2" />
      <circle cx="16" cy="18" r="2" />
    </>,
    props,
  );

export const ActivityIcon = (props: IconProps) =>
  base(<path d="M3 12h4l2.2-7 4 14L15.5 12H21" />, props);

export const AlertTriangleIcon = (props: IconProps) =>
  base(
    <>
      <path d="M12 4 3 20h18L12 4Z" />
      <path d="M12 10v4.5" />
      <path d="M12 17.5v.1" />
    </>,
    props,
  );

export const BellIcon = (props: IconProps) =>
  base(
    <>
      <path d="M7 9a5 5 0 0 1 10 0v4.2c0 .5.2 1 .5 1.4l1 1.2a1 1 0 0 1-.8 1.6H6.3a1 1 0 0 1-.8-1.6l1-1.2c.3-.4.5-.9.5-1.4Z" />
      <path d="M10 19a2 2 0 0 0 4 0" />
    </>,
    props,
  );

export const SettingsIcon = (props: IconProps) =>
  base(
    <>
      <circle cx="12" cy="12" r="3" />
      <path d="M19.4 13.5a1.7 1.7 0 0 0 .3 1.9l.1.1a2 2 0 1 1-2.9 2.9l-.1-.1a1.7 1.7 0 0 0-1.9-.3 1.7 1.7 0 0 0-1 1.6V20a2 2 0 1 1-4 0v-.2a1.7 1.7 0 0 0-1-1.6 1.7 1.7 0 0 0-1.9.3l-.1.1a2 2 0 1 1-2.9-2.9l.1-.1a1.7 1.7 0 0 0 .3-1.9 1.7 1.7 0 0 0-1.6-1H4a2 2 0 1 1 0-4h.2a1.7 1.7 0 0 0 1.6-1 1.7 1.7 0 0 0-.3-1.9l-.1-.1a2 2 0 1 1 2.9-2.9l.1.1a1.7 1.7 0 0 0 1.9.3H10a1.7 1.7 0 0 0 1-1.6V4a2 2 0 1 1 4 0v.2a1.7 1.7 0 0 0 1 1.6 1.7 1.7 0 0 0 1.9-.3l.1-.1a2 2 0 1 1 2.9 2.9l-.1.1a1.7 1.7 0 0 0-.3 1.9V10a1.7 1.7 0 0 0 1.6 1H20a2 2 0 1 1 0 4h-.2a1.7 1.7 0 0 0-1.4 1Z" />
    </>,
    props,
  );

export const SearchIcon = (props: IconProps) =>
  base(
    <>
      <circle cx="11" cy="11" r="7" />
      <path d="m21 21-4.3-4.3" />
    </>,
    props,
  );

export const PaperclipIcon = (props: IconProps) =>
  base(
    <path d="M18.5 8.5 9.9 17.1a3.5 3.5 0 0 1-5-5l8.6-8.6a2.5 2.5 0 0 1 3.5 3.5l-8.4 8.4a1.5 1.5 0 0 1-2-2l7.6-7.6" />,
    props,
  );

export const ImageIcon = (props: IconProps) =>
  base(
    <>
      <rect x="3.5" y="4.5" width="17" height="15" rx="2" />
      <circle cx="9" cy="10" r="1.5" />
      <path d="m5 17 4.5-4.5a1.5 1.5 0 0 1 2.1 0L15 16l1.6-1.6a1.5 1.5 0 0 1 2.1 0L20.5 16" />
    </>,
    props,
  );

export const FilterIcon = (props: IconProps) =>
  base(<path d="M4 6h16M7 12h10M10 18h4" />, props);

export const ChevronDownIcon = (props: IconProps) =>
  base(<path d="m6 9 6 6 6-6" />, props);

export const XIcon = (props: IconProps) => base(<path d="M6 6l12 12M18 6 6 18" />, props);

export const PlusIcon = (props: IconProps) => base(<path d="M12 5v14M5 12h14" />, props);

export const ArrowLeftIcon = (props: IconProps) =>
  base(<path d="M19 12H5m6-7-7 7 7 7" />, props);

export const ArrowDownIcon = (props: IconProps) =>
  base(<path d="M12 5v14m0 0-6-6m6 6 6-6" />, props);

export const TrashIcon = (props: IconProps) =>
  base(
    <>
      <path d="M4.5 7h15" />
      <path d="M9.5 7V5a1.5 1.5 0 0 1 1.5-1.5h2A1.5 1.5 0 0 1 14.5 5v2" />
      <path d="M6.5 7 7.3 19.2A2 2 0 0 0 9.3 21h5.4a2 2 0 0 0 2-1.8L17.5 7" />
      <path d="M10 11v6M14 11v6" />
    </>,
    props,
  );

export const PencilIcon = (props: IconProps) =>
  base(
    <>
      <path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z" />
      <path d="M14.5 5.5 18 9" />
    </>,
    props,
  );

export const UsersIcon = (props: IconProps) =>
  base(
    <>
      <circle cx="9" cy="8.5" r="3.2" />
      <path d="M3.5 19.5a5.5 5.5 0 0 1 11 0" />
      <path d="M15.5 6.2a3.2 3.2 0 0 1 0 6.2" />
      <path d="M17 13.3a5.5 5.5 0 0 1 3.5 6.2" />
    </>,
    props,
  );

export const CheckCircleIcon = (props: IconProps) =>
  base(
    <>
      <circle cx="12" cy="12" r="8.5" />
      <path d="m8.5 12.3 2.4 2.4 4.6-5.2" />
    </>,
    props,
  );

export const MessageIcon = (props: IconProps) =>
  base(
    <path d="M4 5.5h16a1 1 0 0 1 1 1V16a1 1 0 0 1-1 1H9l-4.5 3.5V17H4a1 1 0 0 1-1-1V6.5a1 1 0 0 1 1-1Z" />,
    props,
  );

export const GridIcon = (props: IconProps) =>
  base(
    <>
      <rect x="3.5" y="3.5" width="7.5" height="7.5" rx="1.5" />
      <rect x="13" y="3.5" width="7.5" height="7.5" rx="1.5" />
      <rect x="3.5" y="13" width="7.5" height="7.5" rx="1.5" />
      <rect x="13" y="13" width="7.5" height="7.5" rx="1.5" />
    </>,
    props,
  );

export const LayersIcon = (props: IconProps) =>
  base(
    <>
      <path d="m12 3.5 8.5 4.5-8.5 4.5-8.5-4.5L12 3.5Z" />
      <path d="m3.5 12 8.5 4.5 8.5-4.5" />
      <path d="m3.5 16 8.5 4.5 8.5-4.5" />
    </>,
    props,
  );

export const PowerIcon = (props: IconProps) =>
  base(
    <>
      <path d="M12 3.5v8" />
      <path d="M7 6.3a7.5 7.5 0 1 0 10 0" />
    </>,
    props,
  );

export const KeyIcon = (props: IconProps) =>
  base(
    <>
      <circle cx="8" cy="15" r="4" />
      <path d="m11.2 12 8-8M16.5 6.5l2.5 2.5M13.7 9.3l2 2" />
    </>,
    props,
  );

export const GlobeIcon = (props: IconProps) =>
  base(
    <>
      <circle cx="12" cy="12" r="8.5" />
      <path d="M3.5 12h17M12 3.5c2.6 2.6 3.9 5.4 3.9 8.5s-1.3 5.9-3.9 8.5c-2.6-2.6-3.9-5.4-3.9-8.5S9.4 6.1 12 3.5Z" />
    </>,
    props,
  );

export const FolderIcon = (props: IconProps) =>
  base(
    <path d="M3.5 7a1.5 1.5 0 0 1 1.5-1.5h4.3a1.5 1.5 0 0 1 1.2.6l1.1 1.4H19a1.5 1.5 0 0 1 1.5 1.5v8.5A1.5 1.5 0 0 1 19 19H5a1.5 1.5 0 0 1-1.5-1.5V7Z" />,
    props,
  );

export const EyeIcon = (props: IconProps) =>
  base(
    <>
      <path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12Z" />
      <circle cx="12" cy="12" r="3" />
    </>,
    props,
  );

export const EyeOffIcon = (props: IconProps) =>
  base(
    <>
      <path d="M3.5 3.5 20.5 20.5" />
      <path d="M10.6 5.7A10.6 10.6 0 0 1 12 5.5c6 0 9.5 6.5 9.5 6.5a15.4 15.4 0 0 1-3 3.9M6.6 6.6C4 8.3 2.5 12 2.5 12s3.5 6.5 9.5 6.5a9.7 9.7 0 0 0 3.2-.55" />
      <path d="M9.9 10a3 3 0 0 0 4.15 4.1" />
    </>,
    props,
  );

export const MoreVerticalIcon = (props: IconProps) =>
  base(
    <>
      <circle cx="12" cy="5.5" r="1.2" fill="currentColor" stroke="none" />
      <circle cx="12" cy="12" r="1.2" fill="currentColor" stroke="none" />
      <circle cx="12" cy="18.5" r="1.2" fill="currentColor" stroke="none" />
    </>,
    props,
  );

export const SendIcon = (props: IconProps) =>
  base(
    <>
      <path d="M21 3 3 10.5l7 2.7 2.8 7L21 3Z" />
      <path d="M20.8 3.2 10.4 13.5" />
    </>,
    props,
  );

export const SparklesIcon = (props: IconProps) =>
  base(
    <>
      <path d="M12 3.5 13.4 8.6 18.5 10l-5.1 1.4L12 16.5l-1.4-5.1L5.5 10l5.1-1.4L12 3.5Z" />
      <path d="M19 15.5 19.7 18l2.3.7-2.3.7-.7 2.3-.7-2.3L16 18.7l2.3-.7.7-2.5Z" />
    </>,
    props,
  );

export const PersonIcon = (props: IconProps) =>
  base(
    <>
      <circle cx="12" cy="8" r="3.5" />
      <path d="M5 20a7 7 0 0 1 14 0" />
    </>,
    props,
  );

export const XCircleIcon = (props: IconProps) =>
  base(
    <>
      <circle cx="12" cy="12" r="8.5" />
      <path d="m9 9 6 6M15 9l-6 6" />
    </>,
    props,
  );

export const ReceiptIcon = (props: IconProps) =>
  base(
    <>
      <path d="M6 3.5h12v17l-2.2-1.5-2.2 1.5-2.1-1.5-2.1 1.5-2.2-1.5-1.2.8V3.5Z" />
      <path d="M8.5 8h7M8.5 11.5h7M8.5 15h4.5" />
    </>,
    props,
  );

export const SmileIcon = (props: IconProps) =>
  base(
    <>
      <circle cx="12" cy="12" r="8.5" />
      <path d="M8.5 10.5h.01M15.5 10.5h.01" />
      <path d="M8.5 14.5c1 1.2 2.2 1.8 3.5 1.8s2.5-.6 3.5-1.8" />
    </>,
    props,
  );

export const FileTextIcon = (props: IconProps) =>
  base(
    <>
      <path d="M14 3.5H7A1.5 1.5 0 0 0 5.5 5v14A1.5 1.5 0 0 0 7 20.5h10a1.5 1.5 0 0 0 1.5-1.5V8L14 3.5Z" />
      <path d="M14 3.5V8h4.5M9 12.5h6M9 16h6" />
    </>,
    props,
  );
