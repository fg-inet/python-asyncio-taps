// A TAPS peer built on Apple's Network.framework, for interop with pytaps.
//
// Network.framework is itself a Transport Services implementation (RFC 9623
// Appendix C), so this exercises RFC 9621 Section 3.4 between two independent
// TAPS stacks rather than against a plain socket.
//
//   nwpeer listen  [tcp|tls] <port>              -> prints "PORT <n>", echoes
//   nwpeer connect [tcp|tls] <host> <port> <msg> -> prints "REPLY <text>"
//
// Certificates for the tls mode come from the environment:
//   NWPEER_IDENTITY   PKCS#12 file for the listener
//   NWPEER_P12_PASS   its passphrase

import Foundation
import Network
import Security

func fail(_ message: String) -> Never {
    print("ERROR \(message)")
    fflush(stdout)
    exit(1)
}

func loadIdentity() -> SecIdentity? {
    guard let path = ProcessInfo.processInfo.environment["NWPEER_IDENTITY"],
          let data = FileManager.default.contents(atPath: path) else {
        return nil
    }
    let password = ProcessInfo.processInfo.environment["NWPEER_P12_PASS"] ?? ""
    let options = [kSecImportExportPassphrase as String: password]
    var rawItems: CFArray?
    let status = SecPKCS12Import(data as CFData, options as CFDictionary, &rawItems)
    guard status == errSecSuccess,
          let items = rawItems as? [[String: Any]],
          let first = items.first,
          let identity = first[kSecImportItemIdentity as String] else {
        return nil
    }
    return (identity as! SecIdentity)
}

func parameters(useTLS: Bool, forListener: Bool) -> NWParameters {
    guard useTLS else { return .tcp }
    let options = NWProtocolTLS.Options()
    if forListener {
        guard let identity = loadIdentity(),
              let secIdentity = sec_identity_create(identity) else {
            fail("could not load NWPEER_IDENTITY")
        }
        sec_protocol_options_set_local_identity(
            options.securityProtocolOptions, secIdentity)
    } else {
        // The test CA is not in the system trust store, so accept the peer and
        // let pytaps be the side under test.
        sec_protocol_options_set_verify_block(
            options.securityProtocolOptions,
            { _, _, complete in complete(true) },
            DispatchQueue.global())
    }
    return NWParameters(tls: options)
}

func echo(_ connection: NWConnection) {
    connection.receive(minimumIncompleteLength: 1, maximumLength: 65536) {
        data, _, isComplete, error in
        if let data = data, !data.isEmpty {
            let text = String(data: data, encoding: .utf8) ?? ""
            print("RECEIVED \(text)")
            fflush(stdout)
            var reply = Data("nw-echo:".utf8)
            reply.append(data)
            connection.send(content: reply, completion: .contentProcessed({ _ in
                connection.send(content: nil, isComplete: true,
                                completion: .contentProcessed({ _ in }))
            }))
        }
        if error != nil || isComplete { return }
        echo(connection)
    }
}

func runListener(useTLS: Bool, port: UInt16) {
    let listener: NWListener
    do {
        listener = try NWListener(using: parameters(useTLS: useTLS, forListener: true),
                                  on: NWEndpoint.Port(rawValue: port) ?? .any)
    } catch {
        fail("listener: \(error)")
    }
    listener.newConnectionHandler = { connection in
        connection.stateUpdateHandler = { state in
            if case .ready = state { echo(connection) }
            if case .failed(let error) = state {
                print("ERROR connection \(error)")
                fflush(stdout)
            }
        }
        connection.start(queue: .main)
    }
    listener.stateUpdateHandler = { state in
        switch state {
        case .ready:
            print("PORT \(listener.port?.rawValue ?? 0)")
            fflush(stdout)
        case .failed(let error):
            fail("listener state: \(error)")
        default:
            break
        }
    }
    listener.start(queue: .main)
    dispatchMain()
}

func runClient(useTLS: Bool, host: String, port: UInt16, message: String) {
    let connection = NWConnection(
        host: NWEndpoint.Host(host),
        port: NWEndpoint.Port(rawValue: port)!,
        using: parameters(useTLS: useTLS, forListener: false))

    connection.stateUpdateHandler = { state in
        switch state {
        case .ready:
            connection.send(content: Data(message.utf8),
                            completion: .contentProcessed({ _ in }))
            connection.receive(minimumIncompleteLength: 1, maximumLength: 65536) {
                data, _, _, error in
                if let error = error { fail("receive: \(error)") }
                let text = String(data: data ?? Data(), encoding: .utf8) ?? ""
                print("REPLY \(text)")
                fflush(stdout)
                connection.cancel()
                exit(0)
            }
        case .failed(let error):
            fail("connect: \(error)")
        case .waiting(let error):
            fail("waiting: \(error)")
        default:
            break
        }
    }
    connection.start(queue: .main)
    dispatchMain()
}

let arguments = CommandLine.arguments
guard arguments.count >= 3 else { fail("usage: nwpeer listen|connect ...") }
let mode = arguments[1]
let useTLS = arguments[2] == "tls"

if mode == "listen" {
    guard arguments.count >= 4, let port = UInt16(arguments[3]) else {
        fail("usage: nwpeer listen tcp|tls <port>")
    }
    runListener(useTLS: useTLS, port: port)
} else if mode == "connect" {
    guard arguments.count >= 6, let port = UInt16(arguments[4]) else {
        fail("usage: nwpeer connect tcp|tls <host> <port> <message>")
    }
    runClient(useTLS: useTLS, host: arguments[3], port: port, message: arguments[5])
} else {
    fail("unknown mode \(mode)")
}
