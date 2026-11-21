import android.bluetooth.BluetoothAdapter;
import android.bluetooth.BluetoothDevice;
import android.bluetooth.BluetoothManager;
import android.content.Context;
import android.os.Looper;

/** Temporary system-UID diagnostic. Does not access or print bonding secrets. */
public class BluetoothProbe {
    public static void main(String[] args) throws Exception {
        Looper.prepareMainLooper();
        Class<?> activityThread = Class.forName("android.app.ActivityThread");
        Object thread = activityThread.getMethod("systemMain").invoke(null);
        Context context = (Context) activityThread.getMethod("getSystemContext").invoke(thread);
        BluetoothManager manager = (BluetoothManager) context.getSystemService(Context.BLUETOOTH_SERVICE);
        BluetoothAdapter adapter = manager.getAdapter();
        if (adapter == null || !adapter.isEnabled()) throw new IllegalStateException("Android Bluetooth is unavailable");
        BluetoothDevice device = adapter.getRemoteDevice(args[0]);
        if (args.length > 1 && args[1].equals("remove")) {
            System.out.println("REMOVE_BOND " + device.getClass().getMethod("removeBond").invoke(device));
            System.exit(0);
        }
        System.out.println("INITIAL_BOND_STATE " + device.getBondState());
        System.out.println("CREATE_BOND " + device.createBond());
        for (int i = 0; i < 40; i++) {
            int state = device.getBondState();
            System.out.println("BOND_STATE " + state);
            if (state == BluetoothDevice.BOND_BONDED) { System.out.println("SSP_CONFIRMED"); System.exit(0); }
            if (state == BluetoothDevice.BOND_BONDING) {
                try { device.getClass().getMethod("setPairingConfirmation", boolean.class).invoke(device, true); }
                catch (Exception ignored) { }
            }
            Thread.sleep(500);
        }
        System.out.println("SSP_NOT_CONFIRMED");
        System.exit(2);
    }
}
