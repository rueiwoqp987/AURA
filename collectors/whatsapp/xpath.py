CHAT_NAME_PRIMARY_XPATH = '//*[@resource-id="com.whatsapp:id/conversations_row_contact_name"]'
CHAT_NAME_FALLBACK_XPATH = "//android.widget.TextView"

BLUETOOTH_SHARE_TARGET_XPATH = (
    '//*[@resource-id="com.android.intentresolver:id/text1" and contains(@text, "Bluetooth")]'
)
BLUETOOTH_DEVICE_ITEM_XPATH = '//*[@resource-id="android:id/title"]'
BLUETOOTH_SHARE_TARGET_TEXT_XPATH = '//*[@resource-id="android:id/text1" and contains(@text, "Bluetooth")]'
BLUETOOTH_SHARE_TARGET_PARENT_XPATH = '//*[@resource-id="android:id/text1" and contains(@text, "Bluetooth")]/..'

CHATS_TAB_LABELS = ("Chats",)
MORE_OPTIONS_DESC = ("More options",)
CHAT_MORE_LABELS = ("More",)
EXPORT_CHAT_LABELS = ("Export chat",)
INCLUDE_MEDIA_LABELS = ("Include media",)
BLUETOOTH_LABELS = ("Bluetooth",)

BACK_TO_CHAT_LIST_TEXT_MARKERS = ("Chats",)
BACK_TO_CHAT_LIST_DESC_MARKERS = ("Search",)

NAV_BLACKLIST = {
    "Chats",
    "Updates",
    "Communities",
    "Calls",
    "Status",
    "Settings",
    "Search",
    "Archived",
}
