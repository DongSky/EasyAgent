import UIKit
import WebKit

// Shared Rust state and host effects are kept separate. Persist before executing an action.
final class PortableCore {
    func dispatch(_ request: [String: Any]) throws -> [String: Any] {
        let data = try JSONSerialization.data(withJSONObject: request)
        let text = String(data: data, encoding: .utf8)!
        let pointer = text.withCString { eah_dispatch($0) }!
        defer { eah_free(pointer) }
        let result = try JSONSerialization.jsonObject(with: Data(String(cString: pointer).utf8)) as! [String: Any]
        if let state = result["state"] {
            let stateData = try JSONSerialization.data(withJSONObject: state)
            let file = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0].appendingPathComponent("workflow-state.json")
            try FileManager.default.createDirectory(at: file.deletingLastPathComponent(), withIntermediateDirectories: true)
            try stateData.write(to: file, options: [.atomic, .completeFileProtection])
        }
        return result
    }
}
@main final class AppDelegate: UIResponder, UIApplicationDelegate {
    var window: UIWindow?
    func application(_ application: UIApplication, didFinishLaunchingWithOptions options: [UIApplication.LaunchOptionsKey: Any]?) -> Bool {
        let window = UIWindow(frame: UIScreen.main.bounds)
        window.rootViewController = HubController()
        window.makeKeyAndVisible(); self.window = window; return true
    }
}
final class HubController: UIViewController, WKNavigationDelegate {
    let address = UITextField(); let web = WKWebView()
    override func viewDidLoad() {
        super.viewDidLoad(); view.backgroundColor = .systemBackground
        address.placeholder = "https://你的 Hub 地址"; address.keyboardType = .URL; address.autocapitalizationType = .none
        address.text = UserDefaults.standard.string(forKey: "hubURL")
        let button = UIButton(type: .system); button.setTitle("连接工作室", for: .normal); button.addTarget(self, action: #selector(connect), for: .touchUpInside)
        let offline=UIButton(type: .system);offline.setTitle("运行本机离线流程",for:.normal);offline.addTarget(self,action:#selector(runOffline),for:.touchUpInside)
        let stack = UIStackView(arrangedSubviews: [address, button, offline, web]); stack.axis = .vertical; stack.spacing = 12; stack.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(stack); NSLayoutConstraint.activate([stack.leadingAnchor.constraint(equalTo: view.safeAreaLayoutGuide.leadingAnchor, constant: 12), stack.trailingAnchor.constraint(equalTo: view.safeAreaLayoutGuide.trailingAnchor, constant: -12), stack.topAnchor.constraint(equalTo: view.safeAreaLayoutGuide.topAnchor), stack.bottomAnchor.constraint(equalTo: view.safeAreaLayoutGuide.bottomAnchor)])
        web.navigationDelegate = self
    }
    @objc func runOffline() {
        do { let result=try PortableCore().offlineExample();let data=try JSONSerialization.data(withJSONObject:result,options:.prettyPrinted);let alert=UIAlertController(title:"离线流程已保存",message:String(data:data,encoding:.utf8),preferredStyle:.alert);alert.addAction(UIAlertAction(title:"完成",style:.default));present(alert,animated:true) }
        catch {let alert=UIAlertController(title:"本机执行失败",message:error.localizedDescription,preferredStyle:.alert);alert.addAction(UIAlertAction(title:"关闭",style:.default));present(alert,animated:true)}
    }
    @objc func connect() {
        guard let url = URL(string: address.text ?? ""), url.scheme == "https", url.user == nil, url.password == nil else { return }
        UserDefaults.standard.set(url.absoluteString, forKey: "hubURL"); web.load(URLRequest(url: url))
    }
    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction, decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = navigationAction.request.url else {decisionHandler(.cancel);return}
        if url.host == URL(string: address.text ?? "")?.host {decisionHandler(.allow)} else {decisionHandler(.cancel);UIApplication.shared.open(url)}
    }
}

extension PortableCore {
    func offlineExample() throws -> [String: Any] {
        var reply = try dispatch(["op":"create","run_id":UUID().uuidString,"workflow":["name":"本机离线整理","steps":[["id":"material","kind":"transform","input":["text":"确认搬家日期与纸箱数量"]],["id":"receipt","target":"core.echo","depends_on":["material"],"input":["text":["$ref":"material.text"]]]]],"grants":["core.echo":"read"]])
        reply = try dispatch(["op":"next","state":reply["state"]!])
        reply = try dispatch(["op":"next","state":reply["state"]!])
        for action in reply["actions"] as? [[String: Any]] ?? [] {
            reply = try dispatch(["op":"apply","state":reply["state"]!,"event":["type":"complete","step_id":action["step_id"]!,"invocation_id":action["invocation_id"]!,"output":action["input"]!]])
        }
        return reply
    }
}
